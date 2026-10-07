import type { Env } from '../middleware';
import { SignalWireService } from './signalwire_service';
import { TelnyxService } from './telnyx_service';
import { TwilioService } from './twilio_service';
import { getUserSignalwireCredentials } from './user_signalwire_service';
import { getUserTelnyxCredentials } from './user_telnyx_service';
import { getUserTwilioCredentials } from './user_twilio_service';
import { getUserVonageCredentials } from './user_vonage_service';
import { getUserVoximplantCredentials } from './user_voximplant_service';
import { VonageService } from './vonage_service';
import { VoximplantService } from './voximplant_service';

export interface ProviderCallInput {
  campaignId: number;
  campaignNumberId: number;
  toNumber: string;
  fromNumber: string;
  audioUrl: string | null;
  transferNumber: string;
  press1: boolean;
  dispatchAttemptId?: string | null;
  answerUrl?: string | null;
}

export interface ProviderCallResult {
  callSid: string | null;
  status: string;
  awaitingCallback: boolean;
  diagnostic: string | null;
}
export interface ProviderStatusResult {
  status: string;
  duration: number;
  answeredBy: string | null;
  errorMessage: string;
}

export interface VoiceProviderAdapter {
  readonly provider: string;
  readonly intervalMs: number;
  makeCall(input: ProviderCallInput): Promise<ProviderCallResult>;
  getCallStatus(callSid: string, campaignNumberId: number): Promise<ProviderStatusResult>;
}

const intervals: Record<string, number> = {
  twilio: 1000,
  signalwire: 3000,
  telnyx: 250,
  vonage: 1000,
  voximplant: 1000,
};

export async function createVoiceProviderAdapter(
  env: Env,
  provider: string,
  userId: number,
): Promise<VoiceProviderAdapter> {
  let service: {
    makeCall(options: any): Promise<{
      call_sid: string | null;
      status: string;
      accepted_without_sid?: boolean;
      response_diagnostic?: string;
    }>;
    getCallStatus(callSid: string, campaignNumberId?: number): Promise<{
      status: string; duration: number; answered_by: string | null; error_message: string;
    }>;
  };

  if (provider === 'twilio') {
    const [sid, token] = await getUserTwilioCredentials(env, env.DB, userId);
    service = TwilioService(env, sid, token);
  } else if (provider === 'signalwire') {
    const [project, token, space] = await getUserSignalwireCredentials(env, env.DB, userId);
    service = SignalWireService(env, project, token, space);
  } else if (provider === 'telnyx') {
    const [key, sid] = await getUserTelnyxCredentials(env, env.DB, userId);
    service = TelnyxService(env, key, sid);
  } else if (provider === 'vonage') {
    const [appId, key] = await getUserVonageCredentials(env, env.DB, userId);
    service = VonageService(appId, key);
  } else if (provider === 'voximplant') {
    const credentials = await getUserVoximplantCredentials(env, env.DB, userId);
    if (!credentials) throw new Error('Voximplant credentials not configured');
    service = VoximplantService(env, env.DB, credentials);
  } else {
    throw new Error(`Unsupported voice provider: ${provider}`);
  }

  return {
    provider,
    intervalMs: intervals[provider] ?? 1000,
    async makeCall(input) {
      const result = await service.makeCall({
        toNumber: input.toNumber,
        fromNumber: input.fromNumber,
        audioUrl: input.audioUrl,
        transferNumber: input.transferNumber,
        campaignId: input.campaignId,
        press1ToTalkWithAgent: input.press1,
        answerUrl: input.answerUrl,
        dispatchAttemptId: input.dispatchAttemptId,
        metadata: {
          campaign_number_id: input.campaignNumberId,
          dispatch_attempt_id: input.dispatchAttemptId,
        },
      });
      return {
        callSid: result.call_sid,
        status: result.status,
        awaitingCallback: result.accepted_without_sid === true && result.call_sid === null,
        diagnostic: result.response_diagnostic ?? null,
      };
    },
    async getCallStatus(callSid, campaignNumberId) {
      const result = await service.getCallStatus(callSid, campaignNumberId);
      return {
        status: result.status,
        duration: result.duration,
        answeredBy: result.answered_by,
        errorMessage: result.error_message,
      };
    },
  };
}
