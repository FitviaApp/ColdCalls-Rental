import type { Env } from '../middleware';
import { buildPublicCallbackUrl, validatePublicCallbackUrl } from './callback_url_service';
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
  call_sid: string | null;
  status: string;
  accepted_without_sid?: boolean;
  response_diagnostic?: string;
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

function normalizeSpaceUrl(spaceUrl: string): string {
  let normalized = String(spaceUrl ?? '').trim();
  if (normalized.startsWith('https://')) normalized = normalized.slice('https://'.length);
  else if (normalized.startsWith('http://')) normalized = normalized.slice('http://'.length);
  return normalized.replace(/\/+$/, '');
}

const MAX_PROVIDER_RESPONSE_BYTES = 32 * 1024;

async function readBoundedBody(response: Response): Promise<{ text: string; truncated: boolean }> {
  if (!response.body) return { text: '', truncated: false };
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = '';
  let bytesRead = 0;
  let truncated = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const remaining = MAX_PROVIDER_RESPONSE_BYTES - bytesRead;
      if (remaining <= 0) {
        truncated = true;
        await reader.cancel();
        break;
      }
      const chunk = value.byteLength > remaining ? value.subarray(0, remaining) : value;
      bytesRead += chunk.byteLength;
      text += decoder.decode(chunk, { stream: true });
      if (chunk.byteLength !== value.byteLength) {
        truncated = true;
        await reader.cancel();
        break;
      }
    }
    text += decoder.decode();
  } finally {
    reader.releaseLock();
  }
  return { text, truncated };
}

async function bodyPreview(response: Response): Promise<string> {
  try {
    const { text, truncated } = await readBoundedBody(response);
    const sanitized = sanitizeDiagnosticValue(text.trim());
    if (sanitized) return `${sanitized}${truncated ? ' [truncated]' : ''}`;
  } catch {
    return '';
  }
  return 'no response body';
}

function asInt(value: unknown): number {
  if (value === null || value === undefined || value === '') return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
}

const escapeXml = (value: unknown) => String(value ?? '').replace(/[<>&"']/g, (char) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' })[char]!);

function sanitizeDiagnosticValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text
    .replace(/([?&]token=)[^&\s"']+/gi, '$1[redacted]')
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[redacted-token]')
    .replace(/\+\d{7,15}\b/g, '[redacted-number]')
    .slice(0, 300);
}

function responseMetadata(response: Response): Record<string, string | number | null> {
  return {
    status: response.status,
    content_type: response.headers.get('content-type'),
    content_length: response.headers.get('content-length'),
    request_id: response.headers.get('x-request-id') ?? response.headers.get('x-signalwire-request-id'),
  };
}

function acceptedWithoutSidDiagnostic(response: Response, reason: string): string {
  const requestId = response.headers.get('x-request-id') ?? response.headers.get('x-signalwire-request-id');
  return [
    `SignalWire create call accepted without call SID: status=${response.status}`,
    `reason=${reason}`,
    requestId ? `request_id=${sanitizeDiagnosticValue(requestId)}` : '',
  ].filter(Boolean).join(' ');
}

function diagnosticFromVoiceEvents(payload: Record<string, any>): string {
  const events = Array.isArray(payload.data) ? payload.data : [];
  const rendered = events.map((event: Record<string, any>) => {
    const details = event.details && typeof event.details === 'object' ? event.details : {};
    const fields = Object.entries(details)
      .filter(([key, value]) => value !== null && value !== undefined && /error|reason|cause|status|code|message/i.test(key) && !/url/i.test(key))
      .slice(0, 6)
      .map(([key, value]) => `${key}=${sanitizeDiagnosticValue(value)}`);
    return {
      level: String(event.level ?? '').toLowerCase(),
      text: [sanitizeDiagnosticValue(event.name), ...fields].filter(Boolean).join(' '),
    };
  }).filter((entry: { text: string }) => Boolean(entry.text));
  const prioritized = [
    ...rendered.filter((entry: { text: string }) => /exceed|outbound.call.rate|rate.limit/i.test(entry.text)),
    ...rendered.filter((entry: { level: string; text: string }) =>
      !/exceed|outbound.call.rate|rate.limit/i.test(entry.text)
      && (['error', 'warn'].includes(entry.level) || /fail|error|reject/i.test(entry.text))),
  ];
  return [...new Set(prioritized.map((entry: { text: string }) => entry.text))].slice(0, 6).join('; ').slice(0, 1000);
}

export function SignalWireService(env: Env, projectId: string, apiToken: string, spaceUrl: string) {
  if (!projectId || !apiToken || !spaceUrl) {
    throw new Error('SignalWire credentials not configured');
  }

  const space = normalizeSpaceUrl(spaceUrl);
  const baseUrl = `https://${space}/api/laml/2010-04-01/Accounts/${projectId.trim()}`;
  const auth = `Basic ${btoa(`${projectId.trim()}:${apiToken.trim()}`)}`;
  const jsonHeaders = { Authorization: auth, Accept: 'application/json' };

  async function makeCall(options: MakeCallOptions): Promise<CallResult> {
    const toNumber = options.toNumber;
    const fromNumber = options.fromNumber;
    const audioUrl = options.audioUrl ?? null;
    const transferNumber = options.transferNumber;
    const campaignId = options.campaignId ?? null;
    const press1ToTalkWithAgent = options.press1ToTalkWithAgent ?? false;
    const timeout = options.timeout ?? 60;
    const answerUrl = options.answerUrl ?? null;
    const enableMachineDetection = options.enableMachineDetection ?? true;

    const payload = new URLSearchParams();
    payload.set('To', toNumber);
    payload.set('From', fromNumber);
    payload.set('Timeout', String(Math.trunc(timeout)));
    const campaignNumberId = Number(options.metadata?.campaign_number_id ?? 0);
    if (campaignNumberId) {
      // ponytail: token-only fallback because SignalWire may not sign callbacks;
      // 6h bounds the forge window (calls end well before this).
      const statusToken = await createProviderCallbackToken(env, 'signalwire-status', campaignNumberId, 6 * 3600);
      payload.set('StatusCallback', buildPublicCallbackUrl(
        env.BASE_URL,
        `/api/provider/callback/signalwire/${campaignNumberId}?token=${encodeURIComponent(statusToken)}`,
        'SignalWire',
      ));
      payload.set('StatusCallbackMethod', 'POST');
      for (const event of ['initiated', 'ringing', 'answered', 'completed']) payload.append('StatusCallbackEvent', event);
    }

    if (answerUrl) {
      payload.set('Url', validatePublicCallbackUrl(answerUrl, 'SignalWire'));
    } else if (press1ToTalkWithAgent) {
      if (campaignId === null || campaignId === undefined) {
        throw new Error('campaign_id is required when press_1_to_talk_with_agent is enabled');
      }
      const callbackToken = await createProviderCallbackToken(env, 'signalwire', campaignId);
      payload.set(
        'Url',
        buildPublicCallbackUrl(
          env.BASE_URL ?? '',
          `/api/twiml/${campaignId}?token=${encodeURIComponent(callbackToken)}`,
          'SignalWire',
        ),
      );
    } else if (audioUrl) {
      payload.set(
        'Twiml',
        `<Response>
            <Play>${escapeXml(audioUrl)}</Play>
            <Dial callerId="${escapeXml(fromNumber)}" timeout="30">
                <Number>${escapeXml(transferNumber)}</Number>
            </Dial>
        </Response>`,
      );
    } else {
      payload.set(
        'Twiml',
        `<Response>
            <Dial callerId="${escapeXml(fromNumber)}" timeout="30">
                <Number>${escapeXml(transferNumber)}</Number>
            </Dial>
        </Response>`,
      );
    }

    if (enableMachineDetection && !press1ToTalkWithAgent && !answerUrl) {
      payload.set('MachineDetection', 'Enable');
      payload.set('MachineDetectionTimeout', '5');
      payload.set('MachineDetectionSpeechThreshold', '2400');
      payload.set('MachineDetectionSpeechEndThreshold', '1200');
      payload.set('MachineDetectionSilenceTimeout', '5000');
    }

    let data: Record<string, any> | null = null;
    try {
      const response = await fetch(`${baseUrl}/Calls.json`, {
        method: 'POST',
        headers: {
          Authorization: auth,
          Accept: 'application/json',
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: payload,
        signal: AbortSignal.timeout(30000),
      });
      console.info(JSON.stringify({
        event: 'signalwire_create_call_response',
        ...responseMetadata(response),
      }));
      const { text, truncated } = await readBoundedBody(response);
      if (!response.ok) {
        throw new Error(
          `SignalWire create call failed: status=${response.status} body=${sanitizeDiagnosticValue(text.trim()) || 'no response body'}${truncated ? ' [truncated]' : ''}`,
        );
      }
      const trimmed = text.trim();
      if (!trimmed) {
        return {
          call_sid: null,
          status: 'accepted',
          accepted_without_sid: true,
          response_diagnostic: acceptedWithoutSidDiagnostic(response, 'empty_body'),
        };
      }
      try {
        data = JSON.parse(trimmed) as Record<string, any>;
      } catch {
        return {
          call_sid: null,
          status: 'accepted',
          accepted_without_sid: true,
          response_diagnostic: acceptedWithoutSidDiagnostic(response, truncated ? 'oversized_or_invalid_json' : 'invalid_json'),
        };
      }
      if (!data.sid) {
        return {
          call_sid: null,
          status: String(data.status ?? 'accepted'),
          accepted_without_sid: true,
          response_diagnostic: acceptedWithoutSidDiagnostic(response, 'missing_sid'),
        };
      }
    } catch (error) {
      if (error instanceof Error && error.message.startsWith('SignalWire create call failed:')) {
        throw error;
      }
      throw new Error(`SignalWire create call failed: ${error}`);
    }

    const callSid = data.sid;
    const status = data.status || 'queued';
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
    const response = await fetch(`${baseUrl}/Calls/${callSid}.json`, { headers: jsonHeaders, signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error(`SignalWire fetch call failed: status=${response.status}`);
    const data = (await response.json()) as Record<string, any>;
    const status = String(data.status ?? '').toLowerCase();
    const rawErrorCode = data.error_code ?? data.ErrorCode ?? data.code ?? null;
    const errorCode = rawErrorCode === null || rawErrorCode === undefined || rawErrorCode === ''
      ? null
      : asInt(rawErrorCode);
    const diagnostic = [
      data.error_message ?? data.ErrorMessage ?? data.message,
      data.call_reason ? `call_reason=${data.call_reason}` : '',
      data.hangup_cause ? `hangup_cause=${data.hangup_cause}` : '',
      data.sip_response_code ? `sip_response_code=${data.sip_response_code}` : '',
    ].map((value) => String(value ?? '').trim()).filter(Boolean).join('; ');
    let eventDiagnostic = '';
    if (status === 'failed') {
      try {
        const eventsResponse = await fetch(`https://${space}/api/voice/logs/${encodeURIComponent(callSid)}/events`, {
          headers: jsonHeaders,
          signal: AbortSignal.timeout(30000),
        });
        if (eventsResponse.ok) eventDiagnostic = diagnosticFromVoiceEvents(await eventsResponse.json() as Record<string, any>);
      } catch {
        // Compatibility call status remains authoritative if historical event lookup is unavailable.
      }
    }
    const errorMessage = [diagnostic, eventDiagnostic].filter(Boolean).join('; ') || (status === 'failed'
      ? `SignalWire reported failed${errorCode === null ? '' : ` (error ${errorCode})`}`
      : '');
    return {
      status,
      duration: asInt(data.duration),
      answered_by: data.answered_by ?? data.AnsweredBy ?? null,
      error_code: errorCode,
      error_message: errorMessage,
    };
  }

  async function updateCallTwiml(callSid: string, twiml: string): Promise<void> {
    if (!callSid) throw new Error('call_sid is required');
    const payload = new URLSearchParams();
    payload.set('Twiml', String(twiml ?? '').trim());
    if (!payload.get('Twiml')) throw new Error('Twiml payload is required');
    try {
      const response = await fetch(`${baseUrl}/Calls/${callSid}.json`, {
        method: 'POST',
        headers: {
          Authorization: auth,
          Accept: 'application/json',
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: payload,
        signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) {
        throw new Error(
          `SignalWire update call failed: status=${response.status} body=${await bodyPreview(response)}`,
        );
      }
    } catch (error) {
      if (error instanceof Error && error.message.startsWith('SignalWire update call failed:')) {
        throw error;
      }
      throw new Error(`SignalWire update call failed: ${error}`);
    }
  }

  return { makeCall, getCallStatus, pollCallStatus, updateCallTwiml };
}

export type SignalWireServiceInstance = ReturnType<typeof SignalWireService>;
