import type { Env } from '../middleware';
import { TwilioService } from './twilio_service';
import { getUserTwilioCredentials, hasUserTwilioCredentials } from './user_twilio_service';

export const TWILIO_PLAY_CONTENT_TYPES = new Set([
  'audio/mpeg',
  'audio/mp3',
  'audio/wav',
  'audio/wave',
  'audio/x-wav',
  'audio/aiff',
  'audio/x-aiff',
  'audio/basic',
  'audio/gsm',
  'audio/x-gsm',
  'audio/ulaw',
]);

interface TwilioCampaignReadinessRow {
  id: number;
  user_id: number;
  campaign_mode: string;
  caller_id_id: number;
  caller_number: string;
  audio_id: number | null;
  r2_key: string | null;
  content_type: string | null;
}

export async function ensureTwilioBasicCampaignReady(
  env: Env,
  campaignId: number,
  userId: number,
): Promise<void> {
  const campaign = await env.DB.prepare(
    `SELECT c.id,c.user_id,c.campaign_mode,c.caller_id_id,ci.phone_number caller_number,
            c.audio_id,a.r2_key,a.content_type
       FROM campaigns c
       JOIN caller_ids ci ON ci.id=c.caller_id_id
       LEFT JOIN audios a ON a.id=c.audio_id
      WHERE c.id=? AND c.user_id=? AND c.voice_provider='twilio'`,
  ).bind(campaignId, userId).first<TwilioCampaignReadinessRow>();
  if (!campaign || campaign.campaign_mode !== 'audio') return;
  if (!(await hasUserTwilioCredentials(env, env.DB, userId))) {
    throw new Error('Verify Twilio credentials in Settings before starting this campaign.');
  }

  const [accountSid, authToken] = await getUserTwilioCredentials(env, env.DB, userId);
  const service = TwilioService(env, accountSid, authToken);
  const checkedAt = new Date().toISOString();
  let callerIdValid = false;
  try {
    const result = await service.verifyCallerId(campaign.caller_number);
    callerIdValid = result.valid;
    await env.DB.prepare(
      `INSERT INTO caller_id_provider_status(caller_id_id,user_id,provider,provider_sid,status,checked_at,error)
       VALUES(?,?,'twilio',?,?,?,NULL)
       ON CONFLICT(caller_id_id,provider) DO UPDATE SET provider_sid=excluded.provider_sid,
         status=excluded.status,checked_at=excluded.checked_at,error=NULL,user_id=excluded.user_id`,
    ).bind(campaign.caller_id_id, userId, result.providerSid, result.status, checkedAt).run();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await env.DB.prepare(
      `INSERT INTO caller_id_provider_status(caller_id_id,user_id,provider,status,checked_at,error)
       VALUES(?,?,'twilio','error',?,?)
       ON CONFLICT(caller_id_id,provider) DO UPDATE SET status='error',checked_at=excluded.checked_at,
         error=excluded.error,user_id=excluded.user_id`,
    ).bind(campaign.caller_id_id, userId, checkedAt, message.slice(0, 350)).run();
    throw error;
  }
  if (!callerIdValid) {
    throw new Error('The selected Caller ID is not owned or verified in this Twilio account.');
  }

  if (!campaign.audio_id) return;
  let contentType = String(campaign.content_type ?? '').toLowerCase();
  if (!contentType && campaign.r2_key) {
    const object = await env.AUDIO_R2.head(campaign.r2_key);
    contentType = String(object?.httpMetadata?.contentType ?? '').toLowerCase();
    if (contentType) {
      await env.DB.prepare('UPDATE audios SET content_type=? WHERE id=?').bind(contentType, campaign.audio_id).run();
    }
  }
  if (!TWILIO_PLAY_CONTENT_TYPES.has(contentType)) {
    throw new Error(`Audio type ${contentType || 'unknown'} is not supported by Twilio <Play>. Upload WAV or MP3.`);
  }
}
