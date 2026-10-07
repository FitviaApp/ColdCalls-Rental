import { Hono, type Context } from 'hono';
import { getCookie } from 'hono/cookie';
import { type AppEnv, getUser, requireRental } from '../middleware';
import { getUserSignalwireCredentials } from '../services/user_signalwire_service';
import { getUserTwilioCredentials } from '../services/user_twilio_service';
import { decodeVoximplantCallbackToken } from '../services/voximplant_service';
import { createRealtimeToken } from '../services/realtime_token';
import { providerAudioUrl, verifyAudioToken } from '../services/media_token';
import { createProviderCallbackToken, verifyProviderCallbackToken } from '../services/provider_callback_token';
import { createVoiceProviderAdapter } from '../services/voice_provider_adapter';
import { validateTwilioWebhook } from '../services/twilio_webhook';
import {
  handleTwilioAnswer,
  handleTwilioGather,
  handleTwilioParentStatus,
  handleTwilioTransferStatus,
} from '../services/twilio_basic_call';

export const apiRoutes = new Hono<AppEnv>();
const xml = (body: string) => new Response(body, { headers: { 'content-type': 'application/xml; charset=utf-8', 'cache-control': 'no-store' } });
const escapeXml = (value: unknown) => String(value ?? '').replace(/[<>&"']/g, (char) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' })[char]!);

interface CampaignXmlRow {
  id: number; user_id: number; voice_provider: string; transfer_number: string | null;
  caller_number: string; audio_id: number | null; press_1_to_talk_with_agent: number;
  campaign_mode?: string;
}

interface ProviderCallbackCampaignRow extends CampaignXmlRow {
  campaign_status: string;
  number_status: string;
  number_dispatch_state: string;
}

async function refreshCampaignAfterProviderCallback(
  env: AppEnv['Bindings'],
  campaignId: number,
  reopenCompletedCampaign: boolean,
): Promise<void> {
  const counts = await env.DB.prepare(
    `SELECT COUNT(*) total,
      SUM(CASE WHEN status NOT IN ('pending','claimed','calling') THEN 1 ELSE 0 END) processed,
      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) successful,
      SUM(CASE WHEN status IN ('failed','dispatch_unknown') THEN 1 ELSE 0 END) failed,
      SUM(CASE WHEN status IN ('pending','claimed','calling') THEN 1 ELSE 0 END) remaining
     FROM campaign_numbers WHERE campaign_id=?`,
  ).bind(campaignId).first<{
    total: number; processed: number; successful: number; failed: number; remaining: number;
  }>();
  if (!counts) return;
  const now = new Date().toISOString();
  const complete = Number(counts.remaining) === 0;
  await env.DB.prepare(
    `UPDATE campaigns SET total_numbers=?,processed_numbers=?,successful_calls=?,failed_calls=?,
      status=CASE
        WHEN status='cancelled' THEN status
        WHEN ? AND status='completed' THEN 'running'
        WHEN ? AND status IN ('running','completed') THEN 'completed'
        ELSE status
      END,
      completed_at=CASE
        WHEN ? AND status='completed' THEN NULL
        WHEN ? AND status IN ('running','completed') THEN COALESCE(completed_at,?)
        ELSE completed_at
      END
     WHERE id=?`,
  ).bind(Number(counts.total), Number(counts.processed), Number(counts.successful), Number(counts.failed),
    reopenCompletedCampaign ? 1 : 0, complete ? 1 : 0,
    reopenCompletedCampaign ? 1 : 0, complete ? 1 : 0, now, campaignId).run();
}

async function campaignForXml(c: Context<AppEnv>, campaignId: number): Promise<CampaignXmlRow | null> {
  return c.env.DB.prepare(
    `SELECT c.id,c.user_id,c.voice_provider,u.transfer_number,ci.phone_number caller_number,
      c.audio_id,c.press_1_to_talk_with_agent FROM campaigns c JOIN users u ON u.id=c.user_id
      JOIN caller_ids ci ON ci.id=c.caller_id_id WHERE c.id=?`,
  ).bind(campaignId).first<CampaignXmlRow>();
}

async function validateLamlRequest(c: Context<AppEnv>, campaign: CampaignXmlRow): Promise<boolean> {
  const signature = c.req.header('X-Twilio-Signature') ?? c.req.header('X-SignalWire-Signature');
  if (!signature) return false;
  let token = '';
  if (campaign.voice_provider === 'twilio') {
    [, token] = await getUserTwilioCredentials(c.env, c.env.DB, campaign.user_id);
    return Boolean(await validateTwilioWebhook(c.req.raw, token));
  } else if (campaign.voice_provider === 'signalwire') [, token] = await getUserSignalwireCredentials(c.env, c.env.DB, campaign.user_id);
  else return false;
  if (!token) return false;
  let payload = c.req.url;
  if (c.req.method === 'POST') {
    const form = await c.req.raw.clone().formData();
    for (const key of [...form.keys()].sort()) for (const value of form.getAll(key)) payload += key + String(value);
  }
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(token), { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
  const digest = new Uint8Array(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(payload)));
  let binary = '';
  for (const byte of digest) binary += String.fromCharCode(byte);
  return constantTimeEqual(btoa(binary), signature);
}

async function validateLamlDocumentRequest(c: Context<AppEnv>, campaign: CampaignXmlRow): Promise<boolean> {
  const token = c.req.query('token') ?? '';
  if (campaign.voice_provider === 'signalwire' && token && await verifyProviderCallbackToken(c.env, token, campaign.voice_provider, campaign.id)) return true;
  return validateLamlRequest(c, campaign);
}

function constantTimeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let mismatch = 0;
  for (let i = 0; i < left.length; i += 1) mismatch |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return mismatch === 0;
}

apiRoutes.on(['GET', 'POST'], '/twiml/:campaignId', async (c) => {
  const campaign = await campaignForXml(c, Number(c.req.param('campaignId')));
  if (!campaign) return xml('<Response><Hangup/></Response>');
  if (!(await validateLamlDocumentRequest(c, campaign))) return c.json({ detail: 'Invalid provider signature' }, 403);
  const play = campaign.audio_id ? `<Play>${escapeXml(await providerAudioUrl(c.env, campaign.audio_id))}</Play>` : '';
  if (campaign.press_1_to_talk_with_agent) {
    const callbackToken = c.req.query('token') || await createProviderCallbackToken(c.env, campaign.voice_provider, campaign.id);
    const gatherAction = new URL(
      `/api/twiml/${campaign.id}/gather?token=${encodeURIComponent(callbackToken)}`,
      c.req.url,
    ).toString();
    return xml(`<Response><Gather numDigits="1" timeout="10" action="${escapeXml(gatherAction)}" method="POST">${play}<Say>Press 1 to talk with an agent.</Say></Gather><Hangup/></Response>`);
  }
  return xml(`<Response>${play}<Dial callerId="${escapeXml(campaign.caller_number)}" timeout="30"><Number>${escapeXml(campaign.transfer_number)}</Number></Dial></Response>`);
});

apiRoutes.post('/twiml/:campaignId/gather', async (c) => {
  const campaign = await campaignForXml(c, Number(c.req.param('campaignId')));
  if (!campaign || !(await validateLamlDocumentRequest(c, campaign))) return c.json({ detail: 'Invalid provider signature' }, 403);
  const form = await c.req.formData();
  return String(form.get('Digits') ?? '') === '1'
    ? xml(`<Response><Dial callerId="${escapeXml(campaign.caller_number)}" timeout="30"><Number>${escapeXml(campaign.transfer_number)}</Number></Dial></Response>`)
    : xml('<Response><Hangup/></Response>');
});

async function reconcileTwilioNumber(c: Context<AppEnv>, numberId: number): Promise<void> {
  const row = await c.env.DB.prepare('SELECT campaign_id FROM campaign_numbers WHERE id=?')
    .bind(numberId).first<{ campaign_id: number }>();
  if (row) await coordinator(c.env, row.campaign_id).reconcile(row.campaign_id);
}

apiRoutes.post('/twilio/calls/:numberId/answer', async (c) => {
  const numberId = Number(c.req.param('numberId'));
  const response = await handleTwilioAnswer(c.env, c.req.raw, numberId);
  if (response.status < 400) await reconcileTwilioNumber(c, numberId);
  return response;
});

apiRoutes.post('/twilio/calls/:numberId/gather', async (c) => {
  const numberId = Number(c.req.param('numberId'));
  const response = await handleTwilioGather(c.env, c.req.raw, numberId);
  if (response.status < 400) await reconcileTwilioNumber(c, numberId);
  return response;
});

apiRoutes.post('/twilio/calls/:numberId/status', async (c) => {
  const numberId = Number(c.req.param('numberId'));
  const response = await handleTwilioParentStatus(c.env, c.req.raw, numberId);
  if (response.status < 400) await reconcileTwilioNumber(c, numberId);
  return response;
});

apiRoutes.post('/twilio/calls/:numberId/transfer-status', async (c) => {
  const numberId = Number(c.req.param('numberId'));
  const response = await handleTwilioTransferStatus(c.env, c.req.raw, numberId);
  if (response.status < 400) await reconcileTwilioNumber(c, numberId);
  return response;
});

apiRoutes.on(['GET', 'POST'], '/telnyx/texml/:campaignId', async (c) => {
  const campaignId = Number(c.req.param('campaignId'));
  const campaign = await campaignForXml(c, campaignId);
  if (!campaign || campaign.voice_provider !== 'telnyx' || !(await verifyProviderCallbackToken(c.env, c.req.query('token') ?? '', 'telnyx', campaignId))) return xml('<Response><Hangup/></Response>');
  const play = campaign.audio_id ? `<Play>${escapeXml(await providerAudioUrl(c.env, campaign.audio_id))}</Play>` : '';
  return xml(`<Response>${play}<Dial callerId="${escapeXml(campaign.caller_number)}"><Number>${escapeXml(campaign.transfer_number)}</Number></Dial></Response>`);
});

apiRoutes.post('/voximplant/callback', async (c) => {
  const body = (c.req.header('content-type') ?? '').includes('application/json')
    ? await c.req.json<Record<string, any>>()
    : await c.req.parseBody();
  const token = String(body.callback_token ?? body.token ?? c.req.query('token') ?? '');
  const payload = await decodeVoximplantCallbackToken(c.env, token);
  if (!payload) return c.json({ detail: 'Invalid callback token' }, 401);
  const numberId = Number(payload.sub);
  const eventId = String(body.event_id ?? body.call_sid ?? body.call_id ?? `${numberId}:${body.status}:${body.duration ?? ''}`);
  const inserted = await c.env.DB.prepare("INSERT OR IGNORE INTO provider_events(id,provider,campaign_number_id) VALUES(?,'voximplant',?)").bind(eventId, numberId).run();
  if (!inserted.meta.changes) return c.json({ ok: true, duplicate: true });
  const providerStatus = String(body.status ?? 'unknown').toLowerCase();
  const terminal = ['completed', 'failed', 'busy', 'no_answer', 'cancelled', 'canceled'].includes(providerStatus);
  const status = providerStatus === 'completed' ? 'completed' : terminal ? 'failed' : 'calling';
  await c.env.DB.prepare(
    `UPDATE campaign_numbers SET status=CASE WHEN status IN ('completed','failed','dispatch_unknown','cancelled') THEN status ELSE ? END,
     provider_status=?,duration_seconds=?,answered_by=?,processed_at=CASE WHEN ? AND processed_at IS NULL THEN ? ELSE processed_at END,updated_at=? WHERE id=?`,
  ).bind(status, providerStatus, Number(body.duration ?? 0), body.answered_by ?? null, terminal ? 1 : 0,
    new Date().toISOString(), new Date().toISOString(), numberId).run();
  const row = await c.env.DB.prepare('SELECT campaign_id FROM campaign_numbers WHERE id=?').bind(numberId).first<{ campaign_id: number }>();
  if (row) await coordinator(c.env, row.campaign_id).reconcile(row.campaign_id);
  return c.json({ ok: true });
});

apiRoutes.post('/provider/callback/:provider/:numberId', async (c) => {
  const provider = c.req.param('provider').toLowerCase();
  const numberId = Number(c.req.param('numberId'));
  if (!['twilio', 'signalwire'].includes(provider) || !numberId) return c.json({ detail: 'Unsupported callback' }, 404);
  const campaign = await c.env.DB.prepare(
    `SELECT c.id,c.user_id,c.voice_provider,c.campaign_mode,u.transfer_number,ci.phone_number caller_number,
      c.audio_id,c.press_1_to_talk_with_agent,c.status campaign_status,
      cn.status number_status,cn.dispatch_state number_dispatch_state
      FROM campaign_numbers cn JOIN campaigns c ON c.id=cn.campaign_id
      JOIN users u ON u.id=c.user_id JOIN caller_ids ci ON ci.id=c.caller_id_id WHERE cn.id=? AND c.voice_provider=?`,
  ).bind(numberId, provider).first<ProviderCallbackCampaignRow>();
  if (!campaign) return c.json({ detail: 'Invalid provider signature' }, 403);
  const signalWireTokenValid = provider === 'signalwire' && await verifyProviderCallbackToken(
    c.env,
    c.req.query('token') ?? '',
    'signalwire-status',
    numberId,
  );
  if (!signalWireTokenValid && !(await validateLamlRequest(c, campaign))) {
    return c.json({ detail: 'Invalid provider signature' }, 403);
  }
  const body = await c.req.parseBody();
  const rawStatus = String(body.CallStatus ?? body.call_status ?? body.status ?? 'unknown').toLowerCase();
  const callSid = String(body.CallSid ?? body.call_sid ?? '');
  const eventId = `${provider}:${numberId}:${callSid}:${rawStatus}:${body.SequenceNumber ?? body.sequence_number ?? ''}`;
  const inserted = await c.env.DB.prepare('INSERT OR IGNORE INTO provider_events(id,provider,campaign_number_id) VALUES(?,?,?)')
    .bind(eventId, provider, numberId).run();
  if (!inserted.meta.changes) return provider === 'signalwire' ? xml('<Response/>') : c.json({ ok: true, duplicate: true });
  const terminal = ['completed', 'failed', 'busy', 'no-answer', 'canceled', 'cancelled'].includes(rawStatus);
  const legacyUnverified = provider === 'twilio' && campaign.campaign_mode !== 'ai_agent';
  const status = legacyUnverified
    ? (terminal ? 'failed' : 'calling')
    : rawStatus === 'completed' ? 'completed' : terminal ? 'failed' : 'calling';
  const duration = Number(body.CallDuration ?? body.duration ?? 0);
  const reconcilesAmbiguousSignalWire = provider === 'signalwire'
    && Boolean(callSid)
    && ['awaiting_callback', 'dispatch_unknown'].includes(campaign.number_dispatch_state);
  const reconciledError = terminal && rawStatus !== 'completed'
    ? `SignalWire reported ${rawStatus}`
    : null;
  const now = new Date().toISOString();
  await c.env.DB.prepare(
    `UPDATE campaign_numbers SET status=CASE
        WHEN status='cancelled' THEN status
        WHEN ? THEN ?
        WHEN status IN ('completed','failed','dispatch_unknown') THEN status
        ELSE ?
      END,
      provider_status=?,call_sid=COALESCE(NULLIF(?,''),call_sid),duration_seconds=?,
      outcome_reason=CASE WHEN ?='twilio' AND ? THEN 'legacy_transfer_unverified' ELSE outcome_reason END,
      processed_at=CASE WHEN ? THEN COALESCE(processed_at,?) WHEN ? THEN NULL ELSE processed_at END,
      dispatch_state=CASE
        WHEN status='cancelled' THEN dispatch_state
        WHEN ? THEN 'finished'
        WHEN ? THEN 'dispatched'
        ELSE dispatch_state
      END,
      error_message=CASE WHEN ? THEN ? ELSE error_message END,
      next_action_at=CASE WHEN ? AND NOT ? THEN ? ELSE next_action_at END,
      updated_at=? WHERE id=?`,
  ).bind(reconcilesAmbiguousSignalWire ? 1 : 0, status, status,
    rawStatus, callSid, Number.isFinite(duration) ? Math.trunc(duration) : 0,
    provider, legacyUnverified && terminal ? 1 : 0,
    terminal ? 1 : 0, now, reconcilesAmbiguousSignalWire ? 1 : 0,
    terminal ? 1 : 0, reconcilesAmbiguousSignalWire ? 1 : 0,
    reconcilesAmbiguousSignalWire ? 1 : 0, reconciledError,
    reconcilesAmbiguousSignalWire ? 1 : 0, terminal ? 1 : 0, now,
    now, numberId).run();
  await refreshCampaignAfterProviderCallback(
    c.env,
    campaign.id,
    reconcilesAmbiguousSignalWire && !terminal && campaign.campaign_status === 'completed',
  );
  await coordinator(c.env, campaign.id).reconcile(campaign.id);
  return provider === 'signalwire' ? xml('<Response/>') : c.json({ ok: true });
});

apiRoutes.get('/media/audio/:audioId', async (c) => {
  const audioId = Number(c.req.param('audioId'));
  if (!(await verifyAudioToken(c.env, c.req.query('token') ?? '', audioId))) return c.json({ detail: 'Invalid or expired audio token' }, 401);
  const audio = await c.env.DB.prepare('SELECT r2_key FROM audios WHERE id=? AND is_active=1').bind(audioId).first<{ r2_key: string }>();
  if (!audio) return c.json({ detail: 'Audio not found' }, 404);
  const object = await c.env.AUDIO_R2.get(audio.r2_key);
  if (!object) return c.json({ detail: 'Audio object not found' }, 404);
  return new Response(object.body, { headers: { 'content-type': object.httpMetadata?.contentType ?? 'application/octet-stream', 'cache-control': 'private, no-store' } });
});

apiRoutes.on(['GET', 'POST'], '/ai-runtime/twiml/:numberId', (c) => c.redirect(`/api/ai-realtime/twiml/${c.req.param('numberId')}`, 307));
apiRoutes.post('/ai-runtime/twiml/:numberId/gather', () => xml('<Response><Hangup/></Response>'));
apiRoutes.get('/ai-runtime/audio/:numberId/:token', () => new Response('Not Found', { status: 404 }));

apiRoutes.on(['GET', 'POST'], '/ai-realtime/twiml/:numberId', async (c) => {
  const numberId = Number(c.req.param('numberId'));
  const row = await c.env.DB.prepare(
    `SELECT cn.id,c.id campaign_id,c.user_id,c.voice_provider,u.transfer_number,ci.phone_number caller_number,
      c.audio_id,c.press_1_to_talk_with_agent FROM campaign_numbers cn JOIN campaigns c ON c.id=cn.campaign_id
      JOIN users u ON u.id=c.user_id JOIN caller_ids ci ON ci.id=c.caller_id_id
     WHERE cn.id=? AND c.campaign_mode='ai_agent' AND c.voice_provider='twilio'`,
  ).bind(numberId).first<CampaignXmlRow & { campaign_id: number }>();
  if (!row) return xml('<Response><Hangup/></Response>');
  if (!(await validateLamlRequest(c, { ...row, id: row.campaign_id }))) return c.json({ detail: 'Invalid provider signature' }, 403);
  const token = await createRealtimeToken(c.env, numberId);
  const base = (c.env.BASE_URL ?? new URL(c.req.url).origin).replace(/^http/, 'ws').replace(/\/+$/, '');
  return xml(`<Response><Connect><Stream url="${escapeXml(`${base}/api/ai-realtime/ws/${numberId}?token=${encodeURIComponent(token)}`)}"/></Connect></Response>`);
});

apiRoutes.get('/ai-realtime/session/:numberId/health', async (c) => {
  const id = Number(c.req.param('numberId'));
  if (!(await ownsNumber(c, id))) return c.json({ detail: 'Not authenticated' }, 401);
  return c.json(await realtime(c.env, id).health(id));
});
apiRoutes.post('/ai-realtime/session/:numberId/start', async (c) => {
  const id = Number(c.req.param('numberId'));
  if (!(await csrfOk(c))) return c.json({ detail: 'Invalid CSRF token' }, 403);
  if (!(await ownsNumber(c, id))) return c.json({ detail: 'Not authenticated' }, 401);
  return c.json({ ok: true, ...(await realtime(c.env, id).health(id)) });
});
apiRoutes.post('/ai-realtime/session/:numberId/stop', async (c) => {
  const id = Number(c.req.param('numberId'));
  if (!(await csrfOk(c))) return c.json({ detail: 'Invalid CSRF token' }, 403);
  if (!(await ownsNumber(c, id))) return c.json({ detail: 'Not authenticated' }, 401);
  await realtime(c.env, id).stop(id);
  return c.json({ ok: true });
});
apiRoutes.get('/ai-realtime/ws/:numberId', async (c) => {
  const id = Number(c.req.param('numberId'));
  const url = new URL(c.req.url);
  url.searchParams.set('campaign_number_id', String(id));
  return realtimeRaw(c.env, id).fetch(new Request(url, c.req.raw));
});
apiRoutes.get('/ai-realtime/ws/:numberId/:token', async (c) => {
  const id = Number(c.req.param('numberId'));
  const url = new URL(c.req.url);
  url.searchParams.set('campaign_number_id', String(id));
  url.searchParams.set('token', c.req.param('token'));
  return realtimeRaw(c.env, id).fetch(new Request(url, c.req.raw));
});

const protectedApi = new Hono<AppEnv>();
protectedApi.use('*', requireRental);
protectedApi.get('/stats', async (c) => {
  const userId = c.get('user').id;
  const row = await c.env.DB.prepare(
    `SELECT COUNT(*) total_campaigns, SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) active_campaigns,
      COALESCE(SUM(total_numbers),0) total_calls, COALESCE(SUM(successful_calls),0) successful_calls,
      COALESCE(SUM(total_cost),0) total_cost FROM campaigns WHERE user_id=?`,
  ).bind(userId).first();
  return c.json(row ?? {});
});
protectedApi.get('/campaigns/:id/progress', async (c) => {
  const row = await c.env.DB.prepare('SELECT status,total_numbers,processed_numbers,successful_calls,failed_calls,total_cost FROM campaigns WHERE id=? AND user_id=?')
    .bind(Number(c.req.param('id')), c.get('user').id).first<Record<string, any>>();
  if (!row) return c.json({ detail: 'Campaign not found' }, 404);
  const counts = await c.env.DB.prepare(
    `SELECT COUNT(*) processed, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) successful,
      SUM(CASE WHEN status IN ('failed','dispatch_unknown') THEN 1 ELSE 0 END) failed FROM campaign_numbers WHERE campaign_id=? AND status NOT IN ('pending','claimed','calling')`,
  ).bind(Number(c.req.param('id'))).first<Record<string, any>>();
  const processed = Number(counts?.processed ?? 0);
  return c.json({ ...row, processed_numbers: processed, successful_calls: Number(counts?.successful ?? 0), failed_calls: Number(counts?.failed ?? 0), progress_percent: row.total_numbers ? processed / Number(row.total_numbers) * 100 : 0 });
});
protectedApi.get('/campaigns/:id/numbers', async (c) => {
  const id = Number(c.req.param('id'));
  const campaign = await c.env.DB.prepare('SELECT id,user_id,voice_provider FROM campaigns WHERE id=? AND user_id=?')
    .bind(id, c.get('user').id).first<{ id: number; user_id: number; voice_provider: string }>();
  if (!campaign) return c.json({ detail: 'Campaign not found' }, 404);
  const page = Math.max(1, Number(c.req.query('page')) || 1);
  const perPage = Math.min(500, Math.max(1, Number(c.req.query('per_page')) || 50));
  const [rows, count] = await Promise.all([
    c.env.DB.prepare('SELECT * FROM campaign_numbers WHERE campaign_id=? ORDER BY id LIMIT ? OFFSET ?').bind(id, perPage, (page - 1) * perPage).all<Record<string, any>>(),
    c.env.DB.prepare('SELECT COUNT(*) total FROM campaign_numbers WHERE campaign_id=?').bind(id).first<{ total: number }>(),
  ]);
  const missingDiagnostics = rows.results.filter((row) => row.status === 'failed' && row.call_sid
    && (!row.error_message || String(row.error_message).startsWith('SignalWire reported failed')
      || (String(row.error_message).includes('callback_unsuccessful') && !/exceed|outbound.call.rate|rate.limit/i.test(String(row.error_message)))));
  if (missingDiagnostics.length) {
    try {
      const adapter = await createVoiceProviderAdapter(c.env, campaign.voice_provider, campaign.user_id);
      for (const row of missingDiagnostics.slice(0, 20)) {
        try {
          const status = await adapter.getCallStatus(String(row.call_sid), Number(row.id));
          row.provider_status = status.status || row.provider_status;
          row.duration_seconds = status.duration || row.duration_seconds;
          row.answered_by = status.answeredBy || row.answered_by;
          row.error_message = status.errorMessage || `${campaign.voice_provider} reported a failed call without diagnostic details`;
          await c.env.DB.prepare(
            'UPDATE campaign_numbers SET provider_status=?,duration_seconds=?,answered_by=?,error_message=?,updated_at=? WHERE id=?',
          ).bind(row.provider_status, row.duration_seconds, row.answered_by, row.error_message, new Date().toISOString(), row.id).run();
        } catch {
          // Keep the stored result visible even if the provider diagnostic read is temporarily unavailable.
        }
      }
    } catch {
      // Missing or invalid provider credentials must not hide the campaign numbers page.
    }
  }
  return c.json({ numbers: rows.results, total: count?.total ?? 0, page, per_page: perPage });
});
protectedApi.get('/data/caller-ids', async (c) => c.json((await c.env.DB.prepare('SELECT id,phone_number,description,country_code FROM caller_ids WHERE user_id=? AND is_active=1 ORDER BY phone_number').bind(c.get('user').id).all()).results));
protectedApi.get('/data/countries', async (c) => c.json((await c.env.DB.prepare('SELECT id,code,name,price_per_minute FROM countries WHERE is_active=1 ORDER BY name').all()).results));
protectedApi.get('/data/audios', async (c) => c.json((await c.env.DB.prepare('SELECT id,name,duration_seconds FROM audios WHERE user_id=? AND is_active=1 ORDER BY name').bind(c.get('user').id).all()).results));
apiRoutes.route('/', protectedApi);

function coordinator(env: AppEnv['Bindings'], id: number) {
  return env.CAMPAIGN_COORDINATORS.get(env.CAMPAIGN_COORDINATORS.idFromName(String(id))) as unknown as { reconcile(id: number): Promise<void> };
}
function realtimeRaw(env: AppEnv['Bindings'], id: number) {
  return env.REALTIME_CALLS.get(env.REALTIME_CALLS.idFromName(String(id)));
}
function realtime(env: AppEnv['Bindings'], id: number) {
  return realtimeRaw(env, id) as unknown as { health(id: number): Promise<Record<string, any>>; stop(id: number): Promise<void> };
}

async function ownsNumber(c: Context<AppEnv>, numberId: number): Promise<boolean> {
  const user = await getUser(c);
  if (!user) return false;
  return Boolean(await c.env.DB.prepare(
    'SELECT 1 FROM campaign_numbers cn JOIN campaigns c ON c.id=cn.campaign_id WHERE cn.id=? AND c.user_id=?',
  ).bind(numberId, user.id).first());
}

// Cookie-authenticated POSTs sit in the CSRF-exempt /api/ bucket; check the token here.
async function csrfOk(c: Context<AppEnv>): Promise<boolean> {
  const cookie = getCookie(c, 'csrf_token');
  if (!cookie || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(cookie)) return false;
  const header = c.req.header('x-csrf-token');
  if (header === cookie) return true;
  const contentType = c.req.header('content-type') ?? '';
  if (contentType.includes('application/x-www-form-urlencoded') || contentType.includes('multipart/form-data')) {
    const form = await c.req.raw.clone().formData();
    return form.get('csrf_token') === cookie;
  }
  return false;
}
