import type { Env } from '../middleware';
import { buildPublicCallbackUrl, validatePublicCallbackUrl } from './callback_url_service';
import { createTwilioAttemptToken } from './provider_callback_token';

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

export interface CallDetails {
  sid: string;
  status: string;
  duration: number;
  price: number | null;
  answered_by: string | null;
  start_time: string | null;
  end_time: string | null;
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

function basicAuth(accountSid: string, authToken: string): string {
  return `Basic ${btoa(`${accountSid}:${authToken}`)}`;
}

async function readJson(response: Response): Promise<Record<string, any> | null> {
  try {
    return (await response.json()) as Record<string, any>;
  } catch {
    return null;
  }
}

function asInt(value: unknown): number {
  if (value === null || value === undefined || value === '') return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
}

function asFloat(value: unknown): number {
  if (value === null || value === undefined || value === '') return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function samePhoneNumber(left: unknown, right: unknown): boolean {
  const normalize = (value: unknown) => String(value ?? '').replace(/\D/g, '');
  const normalizedLeft = normalize(left);
  return normalizedLeft.length > 0 && normalizedLeft === normalize(right);
}

function twilioError(response: Response, body: Record<string, any> | null, operation: string): Error {
  const code = body?.code ?? null;
  const message = String(body?.message ?? '').replace(/\+\d{7,15}/g, '[redacted-number]').slice(0, 300);
  return new Error(`Twilio ${operation} failed: status=${response.status} code=${code} message=${message}`);
}

export function TwilioService(env: Env, accountSid: string, authToken: string) {
  if (!accountSid || !authToken) throw new Error('Twilio credentials not configured');

  const baseUrl = `https://api.twilio.com/2010-04-01/Accounts/${accountSid}`;
  const auth = basicAuth(accountSid, authToken);
  const jsonHeaders = { Authorization: auth, Accept: 'application/json' };
  const formHeaders = {
    Authorization: auth,
    'Content-Type': 'application/x-www-form-urlencoded',
  };

  async function fetchCall(callSid: string): Promise<Record<string, any>> {
    const response = await fetch(`${baseUrl}/Calls/${callSid}.json`, {
      headers: jsonHeaders,
    });
    if (!response.ok) {
      const body = await readJson(response);
      throw new Error(body?.message || `Twilio fetch call failed: status=${response.status}`);
    }
    return (await response.json()) as Record<string, any>;
  }

  async function verifyCredentials(): Promise<{ accountSid: string; status: string }> {
    const response = await fetch(`${baseUrl}.json`, { headers: jsonHeaders, signal: AbortSignal.timeout(15000) });
    const body = await readJson(response);
    if (!response.ok) throw twilioError(response, body, 'credential validation');
    if (String(body?.sid ?? '') !== accountSid) throw new Error('Twilio credential validation returned a different account');
    return { accountSid, status: String(body?.status ?? 'active') };
  }

  async function verifyCallerId(phoneNumber: string): Promise<{
    valid: boolean;
    status: 'owned' | 'verified' | 'invalid';
    providerSid: string | null;
  }> {
    // PhoneNumber is an exact-match filter on both endpoints, so the match is on page 1.
    const query = new URLSearchParams({ PhoneNumber: phoneNumber, PageSize: '20' });
    const incoming = await fetch(`${baseUrl}/IncomingPhoneNumbers.json?${query}`, {
      headers: jsonHeaders,
      signal: AbortSignal.timeout(15000),
    });
    const incomingBody = await readJson(incoming);
    if (!incoming.ok) throw twilioError(incoming, incomingBody, 'caller ID ownership validation');
    const owned = Array.isArray(incomingBody?.incoming_phone_numbers)
      ? incomingBody!.incoming_phone_numbers.find((row: Record<string, any>) => samePhoneNumber(row.phone_number, phoneNumber))
      : null;
    if (owned) return { valid: true, status: 'owned', providerSid: String(owned.sid ?? '') || null };

    const outgoing = await fetch(`${baseUrl}/OutgoingCallerIds.json?${new URLSearchParams({ PhoneNumber: phoneNumber })}`, {
      headers: jsonHeaders,
      signal: AbortSignal.timeout(15000),
    });
    const outgoingBody = await readJson(outgoing);
    if (!outgoing.ok) throw twilioError(outgoing, outgoingBody, 'caller ID verification lookup');
    const verified = Array.isArray(outgoingBody?.outgoing_caller_ids)
      ? outgoingBody!.outgoing_caller_ids.find((row: Record<string, any>) => samePhoneNumber(row.phone_number, phoneNumber))
      : null;
    return verified
      ? { valid: true, status: 'verified', providerSid: String(verified.sid ?? '') || null }
      : { valid: false, status: 'invalid', providerSid: null };
  }

  async function getCallStatus(callSid: string): Promise<PollResult> {
    const call = await fetchCall(callSid);
    const rawErrorCode = call.error_code;
    return { status: String(call.status ?? '').toLowerCase(), duration: asInt(call.duration), answered_by: call.answered_by ?? null,
      error_code: rawErrorCode === null || rawErrorCode === undefined || rawErrorCode === '' ? null : asInt(rawErrorCode),
      error_message: String(call.error_message ?? '').trim() };
  }

  async function makeCall(options: MakeCallOptions): Promise<CallResult> {
    const toNumber = options.toNumber;
    const fromNumber = options.fromNumber;
    const timeout = options.timeout ?? 60;
    const answerUrl = options.answerUrl ?? null;
    const enableMachineDetection = options.enableMachineDetection ?? true;
    const dispatchAttemptId = options.dispatchAttemptId ?? String(options.metadata?.dispatch_attempt_id ?? '');

    const form = new URLSearchParams();
    form.set('To', toNumber);
    form.set('From', fromNumber);
    form.set('Timeout', String(timeout));
    const campaignNumberId = Number(options.metadata?.campaign_number_id ?? 0);
    if (campaignNumberId) {
      let statusPath = `/api/provider/callback/twilio/${campaignNumberId}`;
      if (!answerUrl) {
        if (!dispatchAttemptId) throw new Error('dispatch_attempt_id is required for Twilio basic calls');
        const statusToken = await createTwilioAttemptToken(env, {
          numberId: campaignNumberId,
          attemptId: dispatchAttemptId,
          purpose: 'parent-status',
        });
        statusPath = `/api/twilio/calls/${campaignNumberId}/status?token=${encodeURIComponent(statusToken)}`;
      }
      form.set('StatusCallback', buildPublicCallbackUrl(env.BASE_URL, statusPath, 'Twilio'));
      form.set('StatusCallbackMethod', 'POST');
      for (const event of ['initiated', 'ringing', 'answered', 'completed']) form.append('StatusCallbackEvent', event);
    }

    if (answerUrl) {
      form.set('Url', validatePublicCallbackUrl(answerUrl, 'Twilio'));
    } else {
      if (!campaignNumberId || !dispatchAttemptId) {
        throw new Error('campaign_number_id and dispatch_attempt_id are required for Twilio basic calls');
      }
      const callbackToken = await createTwilioAttemptToken(env, {
        numberId: campaignNumberId,
        attemptId: dispatchAttemptId,
        purpose: 'answer',
      });
      form.set(
        'Url',
        buildPublicCallbackUrl(
          env.BASE_URL ?? '',
          `/api/twilio/calls/${campaignNumberId}/answer?token=${encodeURIComponent(callbackToken)}`,
          'Twilio',
        ),
      );
    }

    if (enableMachineDetection && !answerUrl) {
      form.set('MachineDetection', 'Enable');
      form.set('MachineDetectionTimeout', '5');
    }

    const response = await fetch(`${baseUrl}/Calls.json`, {
      method: 'POST',
      headers: formHeaders,
      body: form,
      signal: AbortSignal.timeout(30000),
    });

    if (!response.ok) {
      const body = await readJson(response);
      const errorCode = body?.code ?? null;
      let moreInfo = typeof body?.more_info === 'string' ? body.more_info : '';
      if (!moreInfo && errorCode) moreInfo = `https://www.twilio.com/docs/errors/${errorCode}`;
      const detail =
        `Twilio create call failed: status=${response.status} code=${errorCode} ` +
        `message=${String(body?.message ?? '').replace(/\+\d{7,15}/g, '[redacted-number]').slice(0, 300)} more_info=${moreInfo}`;
      throw new Error(detail);
    }

    const data = (await response.json()) as Record<string, any>;
    if (!data.sid) throw new Error(`Twilio create call returned no call SID: ${JSON.stringify(data)}`);
    return { call_sid: String(data.sid), status: String(data.status ?? '') };
  }

  async function pollCallStatus(
    callSid: string,
    options: PollCallStatusOptions = {},
  ): Promise<PollResult> {
    const result = await getCallStatus(callSid);
    options.statusCallback?.(result.status, result.duration, result.answered_by);
    return result;
  }

  async function updateCallTwiml(callSid: string, twiml: string): Promise<void> {
    if (!callSid) throw new Error('call_sid is required');
    const twimlPayload = String(twiml ?? '').trim();
    if (!twimlPayload) throw new Error('Twiml payload is required');
    try {
      const response = await fetch(`${baseUrl}/Calls/${callSid}.json`, {
        method: 'POST',
        headers: formHeaders,
        body: new URLSearchParams({ Twiml: twimlPayload }),
      });
      if (!response.ok) {
        const body = await readJson(response);
        throw new Error(
          `Twilio update call failed: status=${response.status} code=${body?.code ?? null} message=${body?.message ?? ''}`,
        );
      }
    } catch (error) {
      if (error instanceof Error && error.message.startsWith('Twilio update call failed:')) {
        throw error;
      }
      throw new Error(`Twilio update call failed: ${error}`);
    }
  }

  async function getCallCost(callSid: string): Promise<number> {
    try {
      const call = await fetchCall(callSid);
      return Math.abs(asFloat(call.price));
    } catch {
      return 0;
    }
  }

  async function getCallDetails(callSid: string): Promise<CallDetails | null> {
    try {
      const call = await fetchCall(callSid);
      return {
        sid: String(call.sid),
        status: String(call.status ?? ''),
        duration: asInt(call.duration),
        price: call.price === null || call.price === undefined || call.price === '' ? null : Math.abs(asFloat(call.price)),
        answered_by: call.answered_by ?? null,
        start_time: call.start_time ?? null,
        end_time: call.end_time ?? null,
      };
    } catch {
      return null;
    }
  }

  async function getAccountBalance(): Promise<{ balance: number; currency: string } | null> {
    try {
      const response = await fetch(`${baseUrl}/Balance.json`, { headers: jsonHeaders });
      if (!response.ok) throw new Error(`status=${response.status}`);
      const data = (await response.json()) as Record<string, any>;
      const balance = Number(data.balance);
      if (!Number.isFinite(balance)) throw new Error('invalid balance payload');
      return {
        balance,
        currency: String(data.currency || 'USD'),
      };
    } catch {
      return null;
    }
  }

  return {
    makeCall,
    verifyCredentials,
    verifyCallerId,
    getCallStatus,
    pollCallStatus,
    updateCallTwiml,
    getCallCost,
    getCallDetails,
    getAccountBalance,
  };
}

export type TwilioServiceInstance = ReturnType<typeof TwilioService>;
