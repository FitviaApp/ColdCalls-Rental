import { jwtVerify, SignJWT, jwtKey } from './jwt_key';
import type { Env } from '../middleware';
import type { VoximplantCredentials } from './user_voximplant_service';
import { VoximplantManagementService } from './voximplant_management_service';
import { buildPublicCallbackUrl } from './callback_url_service';

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

interface CampaignNumberPollRow {
  status: string;
  duration_seconds: number | null;
  answered_by: string | null;
}

export function VoximplantService(env: Env, db: D1Database, credentials: VoximplantCredentials) {
  if (!credentials || !credentials.vox_rule_id) {
    throw new Error('Voximplant resources are not provisioned');
  }
  const ruleId = credentials.vox_rule_id;
  const management = VoximplantManagementService(credentials);

  async function createCallbackToken(campaignNumberId: number, campaignId: number | null) {
    const now = Math.floor(Date.now() / 1000);
    return new SignJWT({
      sub: String(campaignNumberId),
      campaign_id: campaignId,
      provider: 'voximplant_callback',
      exp: now + 7200,
    })
      .setProtectedHeader({ alg: 'HS256' })
      .sign(jwtKey(env));
  }

  async function makeCall(options: MakeCallOptions): Promise<CallResult> {
    const metadata = options.metadata ?? {};
    const campaignNumberId = Number(metadata.campaign_number_id ?? 0) || 0;
    if (!campaignNumberId) {
      throw new Error('campaign_number_id is required for Voximplant calls');
    }

    const callbackToken = await createCallbackToken(
      campaignNumberId,
      options.campaignId ?? null,
    );
    const callbackUrl = buildPublicCallbackUrl(
      env.BASE_URL ?? '',
      '/api/voximplant/callback',
      'Voximplant',
    );

    const result = await management.startScenario(ruleId, {
      campaign_id: options.campaignId ?? null,
      campaign_number_id: campaignNumberId,
      to_number: options.toNumber,
      caller_id: options.fromNumber,
      audio_url: options.audioUrl ?? null,
      transfer_number: options.transferNumber,
      press_1_to_talk_with_agent: Boolean(options.press1ToTalkWithAgent),
      status_callback_url: callbackUrl,
      callback_token: callbackToken,
    });

    const callSid =
      result.session_id ||
      result.session_access_url ||
      result.request_id ||
      `voximplant-${campaignNumberId}`;
    return { call_sid: String(callSid), status: 'queued' };
  }

  async function pollCallStatus(
    callSid: string,
    options: PollCallStatusOptions = {},
  ): Promise<PollResult> {
    const metadata = options.metadata ?? {};
    const campaignNumberId = Number(metadata.campaign_number_id ?? 0) || 0;
    if (!campaignNumberId) {
      throw new Error('campaign_number_id is required for Voximplant polling');
    }

    const result = await getCallStatus(callSid, campaignNumberId);
    options.statusCallback?.(result.status, result.duration, result.answered_by);
    return result;
  }

  async function getCallStatus(_callSid: string, campaignNumberId: number): Promise<PollResult> {
    const number=await db.prepare('SELECT status,duration_seconds,answered_by FROM campaign_numbers WHERE id=?').bind(campaignNumberId).first<CampaignNumberPollRow>();
    if(!number) return {status:'failed',duration:0,answered_by:null,error_code:null,error_message:'Campaign number not found'};
    return {status:String(number.status??''),duration:Number(number.duration_seconds??0),answered_by:number.answered_by??null,error_code:null,error_message:''};
  }

  return { makeCall, getCallStatus, pollCallStatus };
}

export type VoximplantServiceInstance = ReturnType<typeof VoximplantService>;

export async function decodeVoximplantCallbackToken(
  env: Env,
  token: string,
): Promise<Record<string, unknown> | null> {
  try {
    const { payload } = await jwtVerify(token, jwtKey(env));
    if (payload.provider !== 'voximplant_callback') return null;
    return payload as Record<string, unknown>;
  } catch {
    return null;
  }
}
