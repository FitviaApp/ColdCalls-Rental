import { signRs256Jwt } from './jwt_sign';

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

export function VonageService(applicationId: string, privateKey: string) {
  if (!applicationId || !privateKey) throw new Error('Vonage credentials not configured');

  const app = applicationId.trim();
  const key = privateKey.trim();
  const baseUrl = 'https://api.nexmo.com/v1/calls';

  async function buildJwt(): Promise<string> {
    const now = Math.floor(Date.now() / 1000);
    return signRs256Jwt(key, {
      application_id: app,
      iat: now,
      exp: now + 600,
      jti: crypto.randomUUID(),
      acl: {
        paths: {
          '/v1/calls/**': {},
          '/v1/users/**': {},
          '/v1/conversations/**': {},
          '/v1/sessions/**': {},
          '/v1/devices/**': {},
          '/v1/image/**': {},
          '/v1/media/**': {},
          '/v1/applications/**': {},
          '/beta/**': {},
        },
      },
    });
  }

  async function makeCall(options: MakeCallOptions): Promise<CallResult> {
    const toNumber = options.toNumber;
    const fromNumber = options.fromNumber;
    const audioUrl = options.audioUrl ?? null;
    const transferNumber = options.transferNumber;
    const press1ToTalkWithAgent = options.press1ToTalkWithAgent ?? false;

    if (press1ToTalkWithAgent) {
      throw new Error(
        'Press 1 flow is currently supported only for Twilio, SignalWire, and Voximplant campaigns',
      );
    }

    const ncco: Array<Record<string, unknown>> = [];
    if (audioUrl) {
      ncco.push({ action: 'stream', streamUrl: [audioUrl] });
    }
    ncco.push({
      action: 'connect',
      from: fromNumber,
      endpoint: [{ type: 'phone', number: transferNumber }],
      timeout: 30,
    });

    const payload = {
      to: [{ type: 'phone', number: toNumber }],
      from: { type: 'phone', number: fromNumber },
      ncco,
    };
    const token = await buildJwt();

    let data: Record<string, any>;
    try {
      const response = await fetch(baseUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) throw new Error(`status=${response.status}`);
      data = (await response.json()) as Record<string, any>;
    } catch (error) {
      throw new Error(`Vonage create call failed: ${error}`);
    }

    const callSid = data.uuid ?? data.conversation_uuid;
    if (!callSid) {
      throw new Error(`Vonage create call returned no call id: ${JSON.stringify(data)}`);
    }

    const status = data.status || 'started';
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
    const token=await buildJwt(); const response=await fetch(`${baseUrl}/${callSid}`,{headers:{Authorization:`Bearer ${token}`},signal:AbortSignal.timeout(30000)});
    if(!response.ok) throw new Error(`Vonage fetch call failed: status=${response.status}`); const data=(await response.json()) as Record<string,any>;
    return {status:String(data.status??'').toLowerCase(),duration:asInt(data.duration),answered_by:null,error_code:null,error_message:''};
  }

  return { makeCall, getCallStatus, pollCallStatus };
}

export type VonageServiceInstance = ReturnType<typeof VonageService>;
