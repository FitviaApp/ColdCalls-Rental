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

interface CallControlRow {
  status: string;
  provider_sequence: number;
  dispatch_attempt_id: string | null;
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
        signal: AbortSignal.timeout(30000),
      });
    } catch (error) {
      throw new Error(`Illyvoip ${path} failed: ${error}`);
    }
    if (!response.ok) throw new Error(`Illyvoip ${path} failed: status=${response.status}`);
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
    const callControlId = String(data.call_control_id ?? '');
    if (!callControlId) {
      throw new Error(`Illyvoip create call returned no call control id: ${JSON.stringify(data)}`);
    }
    if (data.state === 'failed' && data.reconciliation_pending !== true) {
      throw new Error('Illyvoip rejected the call before dialing');
    }
    return { call_sid: callControlId, status: 'queued' };
  }

  async function getCallStatus(callSid: string, campaignNumberId?: number): Promise<PollResult> {
    const data = await request('/voice/call-control/status', { call_control_id: callSid });
    const raw = String(data.call_status ?? data.state ?? '').toLowerCase();
    const answeredAt = data.answered_at ?? null;

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
      } catch {
        // Control actions reuse the dispatch idempotency key; the next poll retries them.
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

  // IllyVoIP has no per-call TwiML/callback document: audio and transfer are issued
  // through call control after the destination answers. provider_sequence tracks the
  // per-answer progress (-1 = answered/pending, 0 = audio sent, 2 = transfer/hangup sent)
  // so repeated status polls never replay an action.
  async function driveAnsweredCall(campaignNumberId: number, callControlId: string): Promise<void> {
    const row = await env.DB.prepare(
      `SELECT cn.status, cn.provider_sequence, cn.dispatch_attempt_id, c.audio_id,
              c.press_1_to_talk_with_agent, u.transfer_number
         FROM campaign_numbers cn JOIN campaigns c ON c.id = cn.campaign_id
         JOIN users u ON u.id = c.user_id
        WHERE cn.id = ?`,
    ).bind(campaignNumberId).first<CallControlRow>();
    if (!row || row.status !== 'calling') return;

    const step = Number(row.provider_sequence ?? -1);
    if (step > 0) return;
    const attempt = String(row.dispatch_attempt_id ?? `number-${campaignNumberId}`);
    const now = () => new Date().toISOString();

    console.log(`[IllyVoIP] driveAnsweredCall: campaignNumberId=${campaignNumberId}, callControlId=${callControlId}, step=${step}, transfer_number=${row.transfer_number}`);

    if (step < 0) {
      const audioUrl = row.audio_id ? await providerAudioUrl(env, row.audio_id) : null;
      if (audioUrl) {
        await request(
          '/voice/call-control/playback',
          { call_control_id: callControlId, audio_url: audioUrl },
          `${attempt}:playback`,
        );
      }
      await env.DB.prepare('UPDATE campaign_numbers SET provider_sequence = 0, updated_at = ? WHERE id = ?')
        .bind(now(), campaignNumberId).run();
    }

    if (!row.transfer_number) {
      console.log(`[IllyVoIP] No transfer_number configured for campaignNumberId=${campaignNumberId}`);
      return;
    }

    if (row.press_1_to_talk_with_agent) {
      const gather = await request(
        '/voice/call-control/gather',
        {
          call_control_id: callControlId,
          input: 'dtmf',
          timeout_ms: 8000,
        },
        `${attempt}:gather`,
      );
      if (!String(gather.digits ?? '').includes('1')) {
        await request(
          '/voice/call-control/hangup',
          { call_control_id: callControlId },
          `${attempt}:hangup`,
        );
        await env.DB.prepare(
          "UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = 'not_pressed', updated_at = ? WHERE id = ?",
        ).bind(now(), campaignNumberId).run();
        return;
      }
    }

    console.log(`[IllyVoIP] Initiating transfer to ${row.transfer_number} for campaignNumberId=${campaignNumberId}`);
    let transfer: Record<string, any>;
    try {
      transfer = await request(
        '/voice/call-control/transfer',
        { call_control_id: callControlId, target: { type: 'pstn', value: row.transfer_number } },
        `${attempt}:transfer`,
      );
    } catch (error) {
      console.error(`[IllyVoIP] Transfer request failed: ${error}`);
      await env.DB.prepare(
        "UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
      ).bind(String(error), now(), campaignNumberId).run();
      return;
    }
    console.log(`[IllyVoIP] Transfer response: ${JSON.stringify(transfer)}`);
    await env.DB.prepare(
      'UPDATE campaign_numbers SET provider_sequence = 2, transfer_status = ?, transfer_call_sid = ?, updated_at = ? WHERE id = ?',
    ).bind(
      String(transfer.transfer?.state ?? 'connecting'),
      String(transfer.transfer_id ?? ''),
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

  return { makeCall, getCallStatus, pollCallStatus };
}

export type IllyvoipServiceInstance = ReturnType<typeof IllyvoipService>;
