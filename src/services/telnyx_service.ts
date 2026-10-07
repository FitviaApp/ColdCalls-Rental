import type { Env } from '../middleware';
import { buildPublicCallbackUrl } from './callback_url_service';
import { createProviderCallbackToken } from './provider_callback_token';

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

function asInt(value: unknown): number {
  if (value === null || value === undefined || value === '') return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
}

export function TelnyxService(env: Env, apiKey: string, accountSid: string) {
  if (!apiKey || !accountSid) throw new Error('Telnyx credentials not configured');

  const key = apiKey.trim();
  const sid = accountSid.trim();
  const baseUrl = 'https://api.telnyx.com/v2/texml';
  const headers = { Authorization: `Bearer ${key}` };

  async function makeCall(options: MakeCallOptions): Promise<CallResult> {
    const toNumber = options.toNumber;
    const fromNumber = options.fromNumber;
    const campaignId = options.campaignId ?? null;
    const press1ToTalkWithAgent = options.press1ToTalkWithAgent ?? false;

    if (press1ToTalkWithAgent) {
      throw new Error(
        'Press 1 flow is currently supported only for Twilio, SignalWire, and Voximplant campaigns',
      );
    }
    if (campaignId === null || campaignId === undefined) {
      throw new Error('campaign_id is required for Telnyx campaigns');
    }

    const callbackToken = await createProviderCallbackToken(env, 'telnyx', campaignId);
    const xmlUrl = buildPublicCallbackUrl(
      env.BASE_URL ?? '',
      `/api/telnyx/texml/${campaignId}?token=${encodeURIComponent(callbackToken)}`,
      'Telnyx',
    );
    const payload = new URLSearchParams();
    payload.set('From', fromNumber);
    payload.set('To', toNumber);
    payload.set('Url', xmlUrl);
    payload.set('Method', 'POST');

    let data: Record<string, any>;
    try {
      const response = await fetch(`${baseUrl}/Accounts/${sid}/Calls`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: payload,
        signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) throw new Error(`status=${response.status}`);
      data = (await response.json()) as Record<string, any>;
    } catch (error) {
      throw new Error(`Telnyx create call failed: ${error}`);
    }

    const callSid =
      data.sid ??
      data.call_sid ??
      data.data?.sid ??
      data.data?.call_sid;
    if (!callSid) {
      throw new Error(`Telnyx create call returned no call SID: ${JSON.stringify(data)}`);
    }

    const status = data.status ?? data.data?.status ?? 'queued';
    return { call_sid: String(callSid), status: String(status) };
  }

  async function pollCallStatus(
    callSid: string,
    options: PollCallStatusOptions = {},
  ): Promise<PollResult> {
    const result = await getCallStatus(callSid);
    options.statusCallback?.(result.status, result.duration, result.answered_by);
    return result;
  }

  async function getCallStatus(callSid: string): Promise<PollResult> {
    const response = await fetch(`${baseUrl}/Accounts/${sid}/Calls/${callSid}`, { headers, signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error(`Telnyx fetch call failed: status=${response.status}`);
    const data = (await response.json()) as Record<string, any>; const row = data.data ?? data;
    return { status: String(row.status ?? '').toLowerCase(), duration: asInt(row.duration), answered_by: row.answered_by ?? null, error_code: null, error_message: '' };
  }

  return { makeCall, getCallStatus, pollCallStatus };
}

export type TelnyxServiceInstance = ReturnType<typeof TelnyxService>;
