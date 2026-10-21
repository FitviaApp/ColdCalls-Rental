import type { Env } from '../middleware';
import { providerAudioUrl } from './media_token';

export interface MakeCallOptions {
  toNumber: string;
  fromNumber: string;
  audioUrl?: string | null;
  transferNumber: string;
  campaignId?: number | null;
  press1ToTalkWithAgent?: boolean;
  timeout?: number;
  metadata?: Record<string, unknown> | null;
  answerUrl?: string | null;
  enableMachineDetection?: boolean;
  dispatchAttemptId?: string | null;
}

export interface CallResult {
  call_sid: string;
  status: string;
}

export interface PollResult {
  status: string;
  duration: number;
  answered_by: string | null;
  error_code: number | null;
  error_message: string;
  retry_delay_ms?: number | null;
}

export type StatusCallback = (
  status: string,
  duration: number,
  answeredBy: string | null,
) => void;

export interface PollCallStatusOptions {
  maxWait?: number;
  pollInterval?: number;
  statusCallback?: StatusCallback;
  metadata?: Record<string, unknown> | null;
}

const API_BASE = 'https://api.illyvoip.com/api/v1';
const ANSWERED_STATES = new Set(['active', 'answered', 'in-progress', 'in_progress']);
const TRANSFER_TERMINAL = new Set(['completed', 'failed', 'canceled', 'cancelled']);
// Bounded answer-action retries: -1 = not started, -2/-3 = retryable failure,
// <= -4 = gave up, > 0 = actions issued or in flight. Never re-drive forever:
// the API replays the cached result for a repeated Idempotency-Key, so re-firing
// the same failed request only hammers the provider while the callee hears silence.
const MIN_DRIVE_STEP = -3;

interface IllyvoipHttpError extends Error {
  status?: number;
  errorCode?: string;
}

function redact(text: string): string {
  return text.replace(/\+\d{7,15}/g, '[redacted-number]');
}

interface CallControlRow {
  status: string;
  provider_sequence: number;
  dispatch_attempt_id: string | null;
  error_message: string | null;
  audio_id: number | null;
  press_1_to_talk_with_agent: number;
  transfer_number: string | null;
}

function secondsBetween(startIso: unknown, endIso: unknown): number {
  const start = Date.parse(String(startIso ?? ''));
  const end = Date.parse(String(endIso ?? ''));
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return 0;
  return Math.round((end - start) / 1000);
}

function terminalStatus(raw: string): string {
  if (['completed', 'answered', 'ended'].includes(raw)) return 'completed';
  if (raw === 'busy') return 'busy';
  if (['no-answer', 'no_answer', 'unanswered'].includes(raw)) return 'no_answer';
  if (['canceled', 'cancelled'].includes(raw)) return 'cancelled';
  return 'failed';
}

function digits(value: unknown): string {
  return String(value ?? '').replace(/\D/g, '');
}

const CDR_NO_ANSWER = new Set(['NO_ANSWER', 'Q850_18', 'Q850_19']);
const CDR_BUSY = new Set(['BUSY', 'USER_BUSY', 'Q850_17']);
const CDR_CONNECTED = new Set(['NORMAL_CLEARING', 'ANSWER', 'ANSWERED']);

export function IllyvoipService(env: Env, apiKey: string) {
  if (!apiKey) throw new Error('Illyvoip credentials not configured');
  const key = apiKey.trim();

  async function request(
    path: string,
    body: Record<string, unknown>,
    idempotencyKey?: string,
    timeoutMs = 30000,
  ): Promise<Record<string, any>> {
    const headers: Record<string, string> = {
      'X-Api-Key': key,
      'Content-Type': 'application/json',
    };
    // Replaying the same dispatch attempt key keeps control actions idempotent.
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey.slice(0, 128);
    let response: Response;
    try {
      response = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      console.error(`[IllyVoIP] ${path} request error: ${error}`);
      throw new Error(`Illyvoip ${path} failed: ${error}`);
    }
    if (!response.ok) {
      const text = await response.text().catch(() => '');
      console.error(`[IllyVoIP] ${path} HTTP error status=${response.status} body=${text}`);
      let message = '';
      let errorCode = '';
      try {
        const parsed = JSON.parse(text);
        message = String(parsed?.message ?? '');
        errorCode = String(parsed?.error_code ?? '');
      } catch {
        // Non-JSON error body; status alone still classifies the failure.
      }
      const error: IllyvoipHttpError = new Error(
        `Illyvoip ${path} failed: status=${response.status}${message ? ` ${message}` : ''}`,
      );
      error.status = response.status;
      error.errorCode = errorCode;
      throw error;
    }
    return (await response.json()) as Record<string, any>;
  }

  async function makeCall(options: MakeCallOptions): Promise<CallResult> {
    const attemptId = String(options.dispatchAttemptId ?? crypto.randomUUID());
    const data = await request(
      '/voice/call-control/calls',
      {
        to: options.toNumber,
        from: options.fromNumber,
        timeout_seconds: options.timeout ?? 60,
      },
      attemptId,
    );
    console.log(`[IllyVoIP] makeCall response=${JSON.stringify(data)}`);
    const callControlId = String(data.call_control_id ?? '');
    if (!callControlId) {
      throw new Error(`Illyvoip create call returned no call control id: ${JSON.stringify(data)}`);
    }
    if (data.state === 'failed' && data.reconciliation_pending !== true) {
      throw new Error('Illyvoip rejected the call before dialing');
    }
    return { call_sid: callControlId, status: 'queued' };
  }

  // A transfer POST only starts the leg; "connecting" is not an answer. Follow the
  // transfer id so a failed handoff is recorded instead of staying stuck at connecting.
  async function refreshTransferStatus(campaignNumberId: number): Promise<void> {
    const row = await env.DB.prepare(
      'SELECT transfer_call_sid, transfer_status FROM campaign_numbers WHERE id = ?',
    ).bind(campaignNumberId).first<{ transfer_call_sid: string | null; transfer_status: string | null }>();
    if (!row?.transfer_call_sid || TRANSFER_TERMINAL.has(String(row.transfer_status ?? ''))) return;

    let data: Record<string, any>;
    try {
      data = await request('/voice/call-control/transfer-status', { transfer_id: row.transfer_call_sid }, undefined, 10_000);
    } catch (error) {
      console.error(`[IllyVoIP] transfer-status failed: ${error}`);
      return;
    }
    const transfer = data.transfer ?? {};
    const state = String(transfer.state ?? '').toLowerCase();
    if (!state) return;
    const message = String(transfer.message ?? '');
    const connected = Boolean(transfer.completed_at) || state === 'completed';
    const failed = Boolean(transfer.failed_at || transfer.canceled_at)
      || state === 'failed' || state === 'canceled' || state === 'cancelled';
    console.log(`[IllyVoIP] Transfer state=${state} message=${JSON.stringify(message)}`);
    const ts = new Date().toISOString();
    await env.DB.prepare(
      `UPDATE campaign_numbers
          SET transfer_status = ?,
              transfer_connected_at = CASE WHEN ? THEN COALESCE(transfer_connected_at, ?) ELSE transfer_connected_at END,
              error_message = CASE WHEN ? THEN ? ELSE error_message END,
              updated_at = ?
        WHERE id = ?`,
    ).bind(state, connected ? 1 : 0, ts, failed ? 1 : 0, redact(message).slice(0, 500), ts, campaignNumberId).run();
  }

  // /status only reports a generic "failed"; the CDR (GET /calls) carries the real
  // hangup_cause (e.g. Q850_19 = destination rang, nobody answered) and
  // failure_reason_code (e.g. callerid_blocked), so campaigns can tell a no-answer
  // apart from a provider/account problem. CDR rows can lag call end by a few
  // seconds: hold the row once (outcome_reason='cdr_pending') before finalizing generic.
  async function cdrOutcome(row: { phone_number: string; claimed_at: string | null }): Promise<Record<string, any> | null> {
    const target = digits(row.phone_number).slice(-9);
    if (target.length < 7) return null;
    const parsedClaimedAt = Date.parse(String(row.claimed_at ?? ''));
    const anchor = Number.isFinite(parsedClaimedAt) ? parsedClaimedAt : Date.now();
    try {
      let cursor = '';
      for (let page = 0; page < 3; page++) {
        const url = new URL(`${API_BASE}/calls`);
        url.searchParams.set('days', '2');
        url.searchParams.set('limit', '500');
        if (cursor) url.searchParams.set('cursor', cursor);
        const response = await fetch(url, {
          headers: { 'X-Api-Key': key },
          signal: AbortSignal.timeout(10_000),
        });
        if (!response.ok) return null;
        const data = (await response.json()) as Record<string, any>;
        const calls = (data.calls ?? []) as Record<string, any>[];
        let best: Record<string, any> | null = null;
        let bestDiff = Number.POSITIVE_INFINITY;
        let sawRecent = false;
        for (const call of calls) {
          const started = Date.parse(String(call.started_at ?? ''));
          if (!Number.isFinite(started)) continue;
          if (started > anchor - 300_000) sawRecent = true;
          const diff = Math.abs(started - anchor);
          if (diff > 300_000 || diff >= bestDiff) continue;
          if (call.direction && call.direction !== 'outgoing') continue;
          if (digits(call.to).slice(-9) !== target) continue;
          best = call;
          bestDiff = diff;
        }
        if (best) return best;
        // CDR pages come newest-first: a whole page older than the window means no match.
        if (!sawRecent || !data.pagination?.has_more) return null;
        cursor = String(data.pagination?.next_cursor ?? '');
        if (!cursor) return null;
      }
    } catch (error) {
      console.error(`[IllyVoIP] CDR lookup failed: ${error}`);
    }
    return null;
  }

  async function terminalWithCdrCause(
    campaignNumberId: number,
    duration: number,
    fallbackReason: string,
  ): Promise<PollResult> {
    const finalize = (status: string, reason: string, finalDuration: number): PollResult => ({
      status,
      duration: finalDuration,
      answered_by: null,
      error_code: null,
      error_message: reason,
    });
    let row: { phone_number: string; claimed_at: string | null; outcome_reason: string | null } | null = null;
    try {
      row = await env.DB.prepare(
        'SELECT phone_number, claimed_at, outcome_reason FROM campaign_numbers WHERE id = ?',
      ).bind(campaignNumberId).first<{ phone_number: string; claimed_at: string | null; outcome_reason: string | null }>();
    } catch (error) {
      console.error(`[IllyVoIP] CDR row lookup failed: ${error}`);
    }
    const existingReason = row?.outcome_reason ?? null;
    const alreadyWaited = existingReason === 'cdr_pending';
    const clearPending = async () => {
      if (alreadyWaited) {
        await env.DB.prepare(
          "UPDATE campaign_numbers SET outcome_reason = NULL, updated_at = ? WHERE id = ? AND outcome_reason = 'cdr_pending'",
        ).bind(new Date().toISOString(), campaignNumberId).run().catch(() => {});
      }
    };
    const cdr = row ? await cdrOutcome(row) : null;
    if (!cdr) {
      if (row && !existingReason) {
        await env.DB.prepare(
          "UPDATE campaign_numbers SET outcome_reason = 'cdr_pending', updated_at = ? WHERE id = ? AND outcome_reason IS NULL",
        ).bind(new Date().toISOString(), campaignNumberId).run().catch(() => {});
        return { ...finalize('calling', '', 0), retry_delay_ms: 4_000 };
      }
      await clearPending();
      return finalize('failed', fallbackReason, duration);
    }
    await clearPending();
    const code = cdr.failure_reason_code != null ? String(cdr.failure_reason_code) : '';
    const cause = cdr.hangup_cause != null ? String(cdr.hangup_cause) : '';
    const reason = (code || cause) && `${code || cause}${code && cause ? ` (${cause})` : ''}` || fallbackReason;
    let status = 'failed';
    if (code === 'no_answer' || CDR_NO_ANSWER.has(cause)) status = 'no_answer';
    else if (code === 'busy' || CDR_BUSY.has(cause)) status = 'busy';
    else if (cdr.status === 'completed' || CDR_CONNECTED.has(cause)) status = 'completed';
    const finalDuration = status === 'completed' && !duration ? Number(cdr.duration_seconds ?? 0) : duration;
    return finalize(status, reason, finalDuration);
  }

  async function getCallStatus(callSid: string, campaignNumberId?: number): Promise<PollResult> {
    const data = await request('/voice/call-control/status', { call_control_id: callSid });
    const raw = String(data.call_status ?? data.state ?? '').toLowerCase();
    const answeredAt = data.answered_at ?? null;
    if (campaignNumberId) await refreshTransferStatus(campaignNumberId);

    if (data.terminal === true) {
      const base = terminalStatus(raw);
      const duration = secondsBetween(answeredAt, data.ended_at);
      const failureReason = String(data.failure_reason ?? '');
      if (campaignNumberId && base === 'failed') {
        return terminalWithCdrCause(campaignNumberId, duration, failureReason);
      }
      return {
        status: base,
        duration,
        answered_by: null,
        error_code: null,
        error_message: failureReason,
      };
    }

    if (campaignNumberId && (answeredAt || ANSWERED_STATES.has(raw))) {
      let retryDelayMs: number | null = null;
      try {
        retryDelayMs = await driveAnsweredCall(campaignNumberId, callSid);
      } catch (error) {
        // Safety net: driveAnsweredCall classifies its own failures; anything
        // landing here (e.g. audio lookup or DB) must not complete the row silently.
        console.error(`[IllyVoIP] driveAnsweredCall error: ${error}`);
        await env.DB.prepare(
          `UPDATE campaign_numbers SET error_message = COALESCE(NULLIF(error_message,''), ?),
             outcome_reason = COALESCE(outcome_reason,'answer_actions_failed'), updated_at = ? WHERE id = ?`,
        ).bind(redact(String(error)).slice(0, 500), new Date().toISOString(), campaignNumberId).run().catch(() => {});
        retryDelayMs = 5_000;
      }
      return {
        status: 'calling',
        duration: secondsBetween(answeredAt, new Date().toISOString()),
        answered_by: null,
        error_code: null,
        error_message: '',
        retry_delay_ms: retryDelayMs,
      };
    }

    return {
      status: raw || 'queued',
      duration: 0,
      answered_by: null,
      error_code: null,
      error_message: '',
    };
  }

  // IllyVoIP has no per-call TwiML/callback document: audio, keypad input and transfer
  // are issued through call control after the destination answers. The /execute action
  // chain runs them in order (playback → gather/transfer), because separate requests
  // race the playback and the DTMF window closes before the prompt is heard.
  // provider_sequence: -1..-3 = drive attempts remaining, <= -4 = gave up (call is
  // hung up instead of leaving the callee in silence), 1 = chain running provider-side,
  // 2 = transfer/hangup issued. Retry attempts use a fresh Idempotency-Key suffix, so a
  // cached failure result is never replayed as the only outcome (docs: repeating a key
  // returns the same result, not another attempt).
  // Returns a retry delay (ms) for the coordinator when the drive failed transiently.
  async function driveAnsweredCall(campaignNumberId: number, callControlId: string): Promise<number | null> {
    const row = await env.DB.prepare(
      `SELECT cn.status, cn.provider_sequence, cn.dispatch_attempt_id,
              cn.error_message, c.audio_id, c.press_1_to_talk_with_agent, u.transfer_number
         FROM campaign_numbers cn JOIN campaigns c ON c.id = cn.campaign_id
         JOIN users u ON u.id = c.user_id
        WHERE cn.id = ?`,
    ).bind(campaignNumberId).first<CallControlRow>();
    if (!row || row.status !== 'calling') return null;

    const step = Number(row.provider_sequence ?? -1);
    if (step > 0 || step < MIN_DRIVE_STEP) return null;
    const attemptNo = -step;
    const attempt = String(row.dispatch_attempt_id ?? `number-${campaignNumberId}`);
    const key = (suffix: string) => `${attempt}:${suffix}:${attemptNo}`;
    const transferNumber = row.transfer_number;
    const now = () => new Date().toISOString();
    const audioUrl = row.audio_id ? await providerAudioUrl(env, row.audio_id) : null;
    if (audioUrl) console.log(`[IllyVoIP] audio_url=${audioUrl}`);
    const playback: Record<string, unknown>[] = audioUrl
      ? [{ type: 'playback', audio_url: audioUrl }]
      : [];

    async function execute(actions: Record<string, unknown>[], idempotencyKey: string): Promise<Record<string, any>[]> {
      const chain = await request(
        '/voice/call-control/execute',
        { call_control_id: callControlId, actions },
        idempotencyKey,
        180_000,
      );
      const results = (chain.results ?? []) as Record<string, any>[];
      const failed = results.find((result) => result.ok === false);
      if (chain.status !== 'success' || failed) {
        throw new Error(`Illyvoip action chain failed: ${JSON.stringify(failed ?? chain.error ?? chain.status)}`);
      }
      return results;
    }

    async function transfer(idempotencyKey: string): Promise<void> {
      if (!transferNumber) return;
      let response: Record<string, any> | null = null;
      let lastError: unknown;
      // The transfer service can return 503 while the gather request is still settling
      // on the provider side. Retry the identical request with the same key: the
      // idempotency key makes it safe if the first attempt was actually accepted.
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        try {
          response = await request(
            '/voice/call-control/transfer',
            { call_control_id: callControlId, target: { type: 'pstn', value: transferNumber } },
            idempotencyKey,
          );
          break;
        } catch (error) {
          lastError = error;
          const httpError = error as IllyvoipHttpError;
          if (attempt < 3 && (httpError.status === 503 || httpError.status === 409)) {
            await new Promise((resolve) => setTimeout(resolve, 1_000 * attempt));
            continue;
          }
          break;
        }
      }
      if (!response) {
        console.error(`[IllyVoIP] Transfer request failed: ${lastError}`);
        await env.DB.prepare(
          'UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = ?, error_message = ?, updated_at = ? WHERE id = ?',
        ).bind('failed', redact(String(lastError)).slice(0, 500), now(), campaignNumberId).run();
        return;
      }
      console.log(`[IllyVoIP] Transfer response: ${JSON.stringify(response)}`);
      await env.DB.prepare(
        `UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = ?, transfer_call_sid = ?,
          error_message = NULL, outcome_reason = NULL, updated_at = ? WHERE id = ?`,
      ).bind(
        String(response.transfer?.state ?? 'connecting'),
        String(response.transfer_id ?? ''),
        now(),
        campaignNumberId,
      ).run();
    }

    // A provider-side chain failure must never loop: count it down, back off, and
    // after the last attempt hang up rather than leave an answered callee in silence.
    async function recordDriveFailure(error: unknown): Promise<number | null> {
      const httpError = error as IllyvoipHttpError;
      const text = redact(String(error)).slice(0, 500);
      const nextStep = step - 1;
      // 409 with a pending request means the chain is still running provider-side
      // (e.g. the worker was redeployed mid-await): never re-fire, just track the call.
      if (httpError.status === 409 && /pending/i.test(`${httpError.errorCode ?? ''} ${text}`)) {
        await env.DB.prepare(
          'UPDATE campaign_numbers SET provider_sequence = 1, error_message = ?, updated_at = ? WHERE id = ?',
        ).bind(text, now(), campaignNumberId).run();
        return 2_000;
      }
      if (nextStep < MIN_DRIVE_STEP) {
        await request('/voice/call-control/hangup', { call_control_id: callControlId }, `${attempt}:giveup`)
          .catch((hangupError) => console.error(`[IllyVoIP] give-up hangup failed: ${hangupError}`));
        await env.DB.prepare(
          `UPDATE campaign_numbers SET provider_sequence = ?, error_message = ?,
            outcome_reason = COALESCE(outcome_reason,'answer_actions_failed'), updated_at = ? WHERE id = ?`,
        ).bind(nextStep, text, now(), campaignNumberId).run();
        return null;
      }
      await env.DB.prepare(
        `UPDATE campaign_numbers SET provider_sequence = ?, error_message = ?,
          outcome_reason = COALESCE(outcome_reason,'answer_actions_failed'), updated_at = ? WHERE id = ?`,
      ).bind(nextStep, text, now(), campaignNumberId).run();
      return Math.min(15_000, 5_000 * attemptNo);
    }

    try {
      if (row.press_1_to_talk_with_agent) {
        // The gather's own prompt audio keeps the input window open while the
        // campaign audio plays, so a "1" pressed mid-audio is captured instead
        // of waiting for a separate playback to finish. No TTS prompt.
        const gather: Record<string, unknown> = {
          type: 'gather',
          input: 'dtmf',
          timeout_ms: 20_000,
        };
        if (audioUrl) gather.prompt_audio_url = audioUrl;
        const results = await execute([gather], key('gather'));
        const digits = String(results.find((result) => result.type === 'gather')?.digits ?? '');
        console.log(`[IllyVoIP] Gather digits=${JSON.stringify(digits)} campaignNumberId=${campaignNumberId}`);
        if (!digits.includes('1')) {
          // The gather outcome is final; a failed hangup must not re-drive the chain.
          await request('/voice/call-control/hangup', { call_control_id: callControlId }, key('hangup'))
            .catch((hangupError) => console.error(`[IllyVoIP] hangup failed: ${hangupError}`));
          await env.DB.prepare(
            `UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = 'not_pressed',
              error_message = NULL, outcome_reason = NULL, updated_at = ? WHERE id = ?`,
          ).bind(now(), campaignNumberId).run();
          return null;
        }
        await transfer(key('transfer'));
        return null;
      }

      if (!transferNumber) {
        if (playback.length) await execute(playback, key('playback'));
        await env.DB.prepare(
          'UPDATE campaign_numbers SET provider_sequence = 2, error_message = NULL, outcome_reason = NULL, updated_at = ? WHERE id = ?',
        ).bind(now(), campaignNumberId).run();
        return null;
      }

      const results = await execute(
        [...playback, { type: 'transfer', target: { type: 'pstn', value: transferNumber } }],
        key('transfer'),
      );
      const response = results.find((result) => result.type === 'transfer') ?? {};
      await env.DB.prepare(
        `UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = ?, transfer_call_sid = ?,
          error_message = NULL, outcome_reason = NULL, updated_at = ? WHERE id = ?`,
      ).bind(
        String(response.transfer?.state ?? 'connecting'),
        String(response.transfer_id ?? ''),
        now(),
        campaignNumberId,
      ).run();
      return null;
    } catch (error) {
      console.error(`[IllyVoIP] driveAnsweredCall error: ${error}`);
      return recordDriveFailure(error);
    }
  }

  async function pollCallStatus(
    callSid: string,
    options: PollCallStatusOptions = {},
  ): Promise<PollResult> {
    const result = await getCallStatus(callSid);
    options.statusCallback?.(result.status, result.duration, result.answered_by);
    return result;
  }

  return { makeCall, getCallStatus, pollCallStatus, driveAnsweredCall };
}

export type IllyvoipServiceInstance = ReturnType<typeof IllyvoipService>;
