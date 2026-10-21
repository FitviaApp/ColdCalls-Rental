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

interface CallControlRow {
  status: string;
  provider_sequence: number;
  dispatch_attempt_id: string | null;
  destination_answered_at: string | null;
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
      throw new Error(`Illyvoip ${path} failed: status=${response.status}`);
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
    ).bind(state, connected ? 1 : 0, ts, failed ? 1 : 0, message, ts, campaignNumberId).run();
  }

  async function getCallStatus(callSid: string, campaignNumberId?: number): Promise<PollResult> {
    const data = await request('/voice/call-control/status', { call_control_id: callSid });
    const raw = String(data.call_status ?? data.state ?? '').toLowerCase();
    const answeredAt = data.answered_at ?? null;
    if (campaignNumberId) await refreshTransferStatus(campaignNumberId);

    if (data.terminal === true) {
      return {
        status: terminalStatus(raw),
        duration: secondsBetween(answeredAt, data.ended_at),
        answered_by: null,
        error_code: null,
        error_message: String(data.failure_reason ?? ''),
      };
    }

    if (campaignNumberId && (answeredAt || ANSWERED_STATES.has(raw))) {
      try {
        await driveAnsweredCall(campaignNumberId, callSid);
      } catch (error) {
        console.error(`[IllyVoIP] driveAnsweredCall error: ${error}`);
        // Keep provider failures visible instead of completing the row silently.
        await env.DB.prepare(
          `UPDATE campaign_numbers SET error_message = COALESCE(NULLIF(error_message,''), ?),
             outcome_reason = COALESCE(outcome_reason,'answer_actions_failed'), updated_at = ? WHERE id = ?`,
        ).bind(String(error).slice(0, 500), new Date().toISOString(), campaignNumberId).run().catch(() => {});
      }
      return {
        status: 'calling',
        duration: secondsBetween(answeredAt, new Date().toISOString()),
        answered_by: null,
        error_code: null,
        error_message: '',
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
  // provider_sequence < 1 = not started, 2 = transfer/hangup issued, so a repeated
  // status poll never replays an action.
  async function driveAnsweredCall(campaignNumberId: number, callControlId: string): Promise<void> {
    const row = await env.DB.prepare(
      `SELECT cn.status, cn.provider_sequence, cn.dispatch_attempt_id, cn.destination_answered_at,
              cn.error_message, c.audio_id, c.press_1_to_talk_with_agent, u.transfer_number
         FROM campaign_numbers cn JOIN campaigns c ON c.id = cn.campaign_id
         JOIN users u ON u.id = c.user_id
        WHERE cn.id = ?`,
    ).bind(campaignNumberId).first<CallControlRow>();
    // The control session can answer before the PSTN destination; only the call
    // report webhook records the real answer, so never play into ringing.
    if (!row || row.status !== 'calling' || !row.destination_answered_at) return;

    const step = Number(row.provider_sequence ?? -1);
    if (step > 0) return;
    const attempt = String(row.dispatch_attempt_id ?? `number-${campaignNumberId}`);
    const transferNumber = row.transfer_number;
    const now = () => new Date().toISOString();
    if (row.error_message) {
      await env.DB.prepare(
        'UPDATE campaign_numbers SET error_message = NULL, outcome_reason = NULL, updated_at = ? WHERE id = ?',
      ).bind(now(), campaignNumberId).run();
    }
    // Prefer the Worker media route for IllyVoIP: the public r2.dev host is a
    // development endpoint and can be slow/rate-limited from provider networks.
    const audioUrl = row.audio_id ? await providerAudioUrl(env, row.audio_id, true) : null;
    if (audioUrl) console.log(`[IllyVoIP] playback audio_url=${audioUrl}`);
    const playback: Record<string, unknown>[] = audioUrl
      ? [{ type: 'playback', audio_url: audioUrl }]
      : [];

    async function execute(actions: Record<string, unknown>[], key: string): Promise<Record<string, any>[]> {
      const chain = await request(
        '/voice/call-control/execute',
        { call_control_id: callControlId, actions },
        key,
        180_000,
      );
      const results = (chain.results ?? []) as Record<string, any>[];
      const failed = results.find((result) => result.ok === false);
      if (chain.status !== 'success' || failed) {
        throw new Error(`Illyvoip action chain failed: ${JSON.stringify(failed ?? chain.error ?? chain.status)}`);
      }
      return results;
    }

    async function transfer(key: string): Promise<void> {
      if (!transferNumber) return;
      let response: Record<string, any>;
      try {
        response = await request(
          '/voice/call-control/transfer',
          { call_control_id: callControlId, target: { type: 'pstn', value: transferNumber } },
          key,
        );
      } catch (error) {
        console.error(`[IllyVoIP] Transfer request failed: ${error}`);
        await env.DB.prepare(
          "UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
        ).bind(String(error), now(), campaignNumberId).run();
        return;
      }
      console.log(`[IllyVoIP] Transfer response: ${JSON.stringify(response)}`);
      await env.DB.prepare(
        'UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = ?, transfer_call_sid = ?, updated_at = ? WHERE id = ?',
      ).bind(
        String(response.transfer?.state ?? 'connecting'),
        String(response.transfer_id ?? ''),
        now(),
        campaignNumberId,
      ).run();
    }

    if (row.press_1_to_talk_with_agent) {
      // Gather must start after the prompt finishes, so chain it with playback.
      // The campaign audio is short (~10s): re-prompt with TTS and listen longer,
      // otherwise a "1" pressed during playback falls outside the gather window.
      const gather = {
        type: 'gather',
        input: 'dtmf',
        prompt_text: 'Press 1 to talk with an agent.',
        timeout_ms: 20_000,
      };
      const results = await execute([...playback, gather], `${attempt}:gather`);
      const digits = String(results.find((result) => result.type === 'gather')?.digits ?? '');
      console.log(`[IllyVoIP] Gather digits=${JSON.stringify(digits)} campaignNumberId=${campaignNumberId}`);
      if (!digits.includes('1')) {
        await request('/voice/call-control/hangup', { call_control_id: callControlId }, `${attempt}:hangup`);
        await env.DB.prepare(
          "UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = 'not_pressed', updated_at = ? WHERE id = ?",
        ).bind(now(), campaignNumberId).run();
        return;
      }
      await transfer(`${attempt}:transfer`);
      return;
    }

    if (!transferNumber) {
      if (playback.length) await execute(playback, `${attempt}:playback`);
      await env.DB.prepare('UPDATE campaign_numbers SET provider_sequence = 2, updated_at = ? WHERE id = ?')
        .bind(now(), campaignNumberId).run();
      return;
    }

    const results = await execute(
      [...playback, { type: 'transfer', target: { type: 'pstn', value: transferNumber } }],
      `${attempt}:transfer`,
    );
    const response = results.find((result) => result.type === 'transfer') ?? {};
    await env.DB.prepare(
      'UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = ?, transfer_call_sid = ?, updated_at = ? WHERE id = ?',
    ).bind(
      String(response.transfer?.state ?? 'connecting'),
      String(response.transfer_id ?? ''),
      now(),
      campaignNumberId,
    ).run();
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
