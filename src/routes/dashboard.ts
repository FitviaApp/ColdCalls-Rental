import { Hono, type Context } from 'hono';
import { hashPassword, verifyPassword } from '../auth';
import { render } from '../render';
import { type AppEnv, requireRental } from '../middleware';
import { getActiveRental } from '../services/rental';
import { getAiSchemaHealth } from '../services/ai_campaign_readiness_service';
import { getUserVoiceProviderStatus, hasAnyUserVoiceProviderCredentials, hasUserAiRuntimeCredentials } from '../services/user_voice_provider_service';
import { getUserTwilioCredentials, getUserTwilioVerification, hasUserTwilioCredentials, recordUserTwilioVerificationError, upsertUserTwilioCredentials } from '../services/user_twilio_service';
import { hasUserSignalwireCredentials, upsertUserSignalwireCredentials } from '../services/user_signalwire_service';
import { hasUserTelnyxCredentials, upsertUserTelnyxCredentials } from '../services/user_telnyx_service';
import { hasUserVonageCredentials, upsertUserVonageCredentials } from '../services/user_vonage_service';
import { getUserVoximplantCredentials, hasUserVoximplantCredentials, upsertUserVoximplantCredentials, type UserVoximplantCredentialRow } from '../services/user_voximplant_service';
import { hasUserOpenaiCredentials, upsertUserOpenaiCredentials } from '../services/user_openai_service';
import { hasUserElevenlabsCredentials, upsertUserElevenlabsCredentials } from '../services/user_elevenlabs_service';
import { ensureUserVoximplantResources } from '../services/voximplant_management_service';
import { TwilioService } from '../services/twilio_service';
import { pyDate } from '../date';

export const dashboardRoutes = new Hono<AppEnv>();
dashboardRoutes.use('*', requireRental);

const truthy = (value: string | undefined): boolean => ['1', 'true', 'yes'].includes(String(value ?? '').toLowerCase());
const redirectNotice = (key: string) => `/dashboard/settings?${key}=true`;

async function settingsContext(c: Context<AppEnv>, error: string | null = null) {
  const user = c.get('user');
  const vox = await getUserVoximplantCredentials(c.env, c.env.DB, user.id);
  const twilioVerification = await getUserTwilioVerification(c.env.DB, user.id);
  return {
    request: { url: c.req.url }, user: { ...user, created_at: pyDate(user.created_at) }, error,
    saved: truthy(c.req.query('saved')),
    twilio_saved: truthy(c.req.query('twilio_saved')),
    signalwire_saved: truthy(c.req.query('signalwire_saved')),
    telnyx_saved: truthy(c.req.query('telnyx_saved')),
    vonage_saved: truthy(c.req.query('vonage_saved')),
    voximplant_saved: truthy(c.req.query('voximplant_saved')),
    voximplant_provisioned: truthy(c.req.query('voximplant_provisioned')),
    openai_saved: truthy(c.req.query('openai_saved')),
    elevenlabs_saved: truthy(c.req.query('elevenlabs_saved')),
    password_saved: truthy(c.req.query('password_saved')),
    twilio_configured: await hasUserTwilioCredentials(c.env, c.env.DB, user.id),
    twilio_verification_status: twilioVerification?.verification_status ?? 'not_configured',
    twilio_verified_at: twilioVerification?.verified_at ?? null,
    twilio_verification_error: twilioVerification?.verification_error ?? null,
    signalwire_configured: await hasUserSignalwireCredentials(c.env, c.env.DB, user.id),
    telnyx_configured: await hasUserTelnyxCredentials(c.env, c.env.DB, user.id),
    vonage_configured: await hasUserVonageCredentials(c.env, c.env.DB, user.id),
    voximplant_configured: await hasUserVoximplantCredentials(c.env, c.env.DB, user.id),
    openai_configured: await hasUserOpenaiCredentials(c.env, c.env.DB, user.id),
    elevenlabs_configured: await hasUserElevenlabsCredentials(c.env, c.env.DB, user.id),
    voximplant_status: vox?.provision_status ?? null,
    voximplant_error: vox?.provision_error ?? null,
  };
}

async function settingsError(c: Context<AppEnv>, message: string) {
  return c.html(render('dashboard/settings.html', await settingsContext(c, message)), 400);
}

dashboardRoutes.get('/', async (c) => {
  const user = c.get('user');
  const db = c.env.DB;
  const campaignsResult = await db.prepare(`SELECT c.*,a.name agent_name FROM campaigns c
    LEFT JOIN ai_agents a ON a.id=c.ai_agent_id WHERE c.user_id = ? ORDER BY c.created_at DESC LIMIT 5`).bind(user.id).all<Record<string, any>>();
  const totals = await db.prepare(`SELECT COUNT(*) total_campaigns,
      SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) active_campaigns,
      COALESCE(SUM(processed_numbers),0) total_calls,
      COALESCE(SUM(successful_calls),0) successful_calls,
      COALESCE(SUM(total_cost),0) total_spent,
      SUM(CASE WHEN campaign_mode = 'ai_agent' THEN 1 ELSE 0 END) ai_campaigns
    FROM campaigns WHERE user_id = ?`).bind(user.id).first<Record<string, number>>();
  const ai = await db.prepare(`SELECT COUNT(*) numbers,
      SUM(CASE WHEN cn.ai_handoff_reason IS NOT NULL THEN 1 ELSE 0 END) handoffs,
      SUM(CASE WHEN cn.ai_runtime_error IS NOT NULL THEN 1 ELSE 0 END) errors,
      COALESCE(SUM(cn.ai_no_input_turns),0) silent_turns,
      COALESCE(AVG(cn.ai_turn_count),0) avg_turns
    FROM campaign_numbers cn JOIN campaigns c ON c.id=cn.campaign_id
    WHERE c.user_id=? AND c.campaign_mode='ai_agent'`).bind(user.id).first<Record<string, number>>();
  const agents = await db.prepare(`SELECT COUNT(*) total, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) active FROM ai_agents WHERE user_id=?`).bind(user.id).first<{total:number;active:number}>();
  const latestError = await db.prepare(`SELECT cn.ai_runtime_error value FROM campaign_numbers cn JOIN campaigns c ON c.id=cn.campaign_id WHERE c.user_id=? AND cn.ai_runtime_error IS NOT NULL ORDER BY cn.updated_at DESC LIMIT 1`).bind(user.id).first<{value:string}>();
  const providerStatus = await getUserVoiceProviderStatus(c.env, db, user.id);
  const twilioConfigured = providerStatus.find((p) => p.value === 'twilio')?.configured ?? false;
  let twilioBalance: number | null = null;
  let twilioBalanceCurrency = 'USD';
  let twilioBalanceError: string | null = null;
  if (twilioConfigured) {
    try {
      const [sid, token] = await getUserTwilioCredentials(c.env, db, user.id);
      const balance = await TwilioService(c.env, sid, token).getAccountBalance();
      if (balance) { twilioBalance = balance.balance; twilioBalanceCurrency = balance.currency; }
      else twilioBalanceError = 'Unable to load Twilio balance right now.';
    } catch { twilioBalanceError = 'Unable to load Twilio balance right now.'; }
  }
  const activeRental = await getActiveRental(db, user.id);
  const schema = await getAiSchemaHealth(c.env);
  const campaigns = campaignsResult.results.map((row) => ({
    ...row,
    status: { value: row.status }, campaign_mode: { value: row.campaign_mode }, voice_provider: { value: row.voice_provider },
    progress_percent: row.total_numbers ? Number(row.processed_numbers) / Number(row.total_numbers) * 100 : 0,
    ai_agent: row.agent_name ? { name: row.agent_name } : null,
  }));
  const stats = {
    ...(totals ?? {}), transfer_configured: Boolean(user.transfer_number && await hasAnyUserVoiceProviderCredentials(c.env, db, user.id)),
    rental_active: Boolean(activeRental), twilio_configured: twilioConfigured,
    voximplant_configured: providerStatus.find((p) => p.value === 'voximplant')?.configured ?? false,
    openai_configured: await hasUserOpenaiCredentials(c.env, db, user.id),
    elevenlabs_configured: await hasUserElevenlabsCredentials(c.env, db, user.id),
    ai_runtime_configured: await hasUserAiRuntimeCredentials(c.env, db, user.id),
    worker_online: true, worker_age_seconds: 0, worker_last_heartbeat_at: new Date().toISOString(),
    durable_objects_available: true, durable_objects_detail: 'Campaign and realtime coordinators bound', ai_schema_ready: schema.ready,
    ai_schema_missing_items: schema.missing_items, twilio_balance: twilioBalance,
    twilio_balance_currency: twilioBalanceCurrency, twilio_balance_error: twilioBalanceError,
    ai_agents_total: agents?.total ?? 0, ai_agents_active: agents?.active ?? 0,
    ai_runtime_numbers: ai?.numbers ?? 0, ai_runtime_handoffs: ai?.handoffs ?? 0,
    ai_runtime_errors: ai?.errors ?? 0, ai_runtime_silent_turns: ai?.silent_turns ?? 0,
    ai_runtime_avg_turns: ai?.avg_turns ?? 0, latest_ai_runtime_error: latestError?.value ?? null,
  };
  return c.html(render('dashboard/index.html', { request: { url: c.req.url }, user, campaigns, recent_ai_campaigns: campaigns.filter((x) => x.campaign_mode.value === 'ai_agent').slice(0, 3), stats, active_rental: activeRental ? { ...activeRental, expires_at: pyDate(activeRental.expires_at) } : null }));
});

dashboardRoutes.get('/settings', async (c) => c.html(render('dashboard/settings.html', await settingsContext(c))));

dashboardRoutes.post('/settings/password', async (c) => {
  const body = await c.req.parseBody(); const user = c.get('user');
  const current = String(body.current_password ?? '').trim(); const password = String(body.new_password ?? '').trim(); const confirm = String(body.confirm_password ?? '').trim();
  if (!verifyPassword(current, user.password_hash)) return settingsError(c, 'Current password is incorrect.');
  if (password.length < 6) return settingsError(c, 'New password must be at least 6 characters.');
  if (password !== confirm) return settingsError(c, 'New password and confirmation do not match.');
  if (verifyPassword(password, user.password_hash)) return settingsError(c, 'New password must be different from current password.');
  await c.env.DB.prepare('UPDATE users SET password_hash=?, updated_at=? WHERE id=?').bind(hashPassword(password), new Date().toISOString(), user.id).run();
  return c.redirect(redirectNotice('password_saved'), 302);
});

dashboardRoutes.post('/settings/transfer', async (c) => {
  const number = String((await c.req.parseBody()).transfer_number ?? '').trim();
  if (!/^\+[1-9]\d{1,14}$/.test(number)) return settingsError(c, 'Invalid transfer number. Use E.164 format (e.g., +15551234567)');
  await c.env.DB.prepare('UPDATE users SET transfer_number=?, updated_at=? WHERE id=?').bind(number, new Date().toISOString(), c.get('user').id).run();
  return c.redirect(redirectNotice('saved'), 302);
});

dashboardRoutes.post('/settings/twilio', async (c) => {
  const b = await c.req.parseBody();
  const sid = String(b.account_sid ?? '').trim();
  const token = String(b.auth_token ?? '').trim();
  if (!/^AC[0-9a-fA-F]{32}$/.test(sid)) return settingsError(c, 'Invalid Twilio Account SID format.');
  if (!token) return settingsError(c, 'Twilio Auth Token cannot be empty.');
  try {
    await TwilioService(c.env, sid, token).verifyCredentials();
    const verifiedAt = new Date().toISOString();
    await upsertUserTwilioCredentials(c.env, c.env.DB, c.get('user').id, sid, token, {
      status: 'verified',
      verifiedAt,
    });
    const [storedSid, storedToken] = await getUserTwilioCredentials(c.env, c.env.DB, c.get('user').id);
    const storedStatus = await getUserTwilioVerification(c.env.DB, c.get('user').id);
    if (storedSid !== sid || storedToken !== token || storedStatus?.verification_status !== 'verified') {
      throw new Error('Credential readback failed');
    }
    return c.redirect(redirectNotice('twilio_saved'), 302);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown validation error';
    await recordUserTwilioVerificationError(c.env.DB, c.get('user').id, message);
    return settingsError(c, `Twilio credentials were not saved: ${message.slice(0, 350)}`);
  }
});
dashboardRoutes.post('/settings/signalwire', async (c) => { const b=await c.req.parseBody(); const project=String(b.project_id??'').trim(),token=String(b.api_token??'').trim(),space=String(b.space_url??'').trim(); if(!project||!token||!space) return settingsError(c,'SignalWire Project ID, API Token, and Space URL are required.'); await upsertUserSignalwireCredentials(c.env,c.env.DB,c.get('user').id,project,token,space); return c.redirect(redirectNotice('signalwire_saved'),302); });
dashboardRoutes.post('/settings/telnyx', async (c) => { const b=await c.req.parseBody(); const key=String(b.api_key??'').trim(),sid=String(b.account_sid??'').trim(); if(!key||!sid) return settingsError(c,'Telnyx API Key and Account SID are required.'); await upsertUserTelnyxCredentials(c.env,c.env.DB,c.get('user').id,key,sid); return c.redirect(redirectNotice('telnyx_saved'),302); });
dashboardRoutes.post('/settings/vonage', async (c) => { const b=await c.req.parseBody(); const app=String(b.application_id??'').trim(),key=String(b.private_key??'').trim(); if(!app) return settingsError(c,'Vonage Application ID cannot be empty.'); if(!key.includes('BEGIN')) return settingsError(c,'Vonage private key must be in PEM format.'); await upsertUserVonageCredentials(c.env,c.env.DB,c.get('user').id,app,key); return c.redirect(redirectNotice('vonage_saved'),302); });
dashboardRoutes.post('/settings/openai', async (c) => { const b=await c.req.parseBody(); const key=String(b.api_key??'').trim(); if(!key.startsWith('sk-')) return settingsError(c,'OpenAI API key must start with sk-.'); await upsertUserOpenaiCredentials(c.env,c.env.DB,c.get('user').id,key,String(b.organization_id??'')); return c.redirect(redirectNotice('openai_saved'),302); });
dashboardRoutes.post('/settings/elevenlabs', async (c) => { const key=String((await c.req.parseBody()).api_key??'').trim(); if(!key) return settingsError(c,'ElevenLabs API key cannot be empty.'); await upsertUserElevenlabsCredentials(c.env,c.env.DB,c.get('user').id,key); return c.redirect(redirectNotice('elevenlabs_saved'),302); });

async function provisionVox(c: Context<AppEnv>, row: UserVoximplantCredentialRow) {
  const credentials=await getUserVoximplantCredentials(c.env,c.env.DB,c.get('user').id); if(!credentials) return settingsError(c,'Configure Voximplant credentials first.');
  try { await ensureUserVoximplantResources(c.env.DB,c.get('user'),row,credentials); return c.redirect('/dashboard/settings?voximplant_saved=true&voximplant_provisioned=true',302); }
  catch(e){ const message=String(e instanceof Error?e.message:e).slice(0,500); await c.env.DB.prepare("UPDATE user_voximplant_credentials SET provision_status='failed', provision_error=?, updated_at=? WHERE id=?").bind(message,new Date().toISOString(),row.id).run(); return settingsError(c,`Voximplant provisioning failed: ${message}`); }
}
dashboardRoutes.post('/settings/voximplant', async (c) => { const b=await c.req.parseBody(); const accountId=String(b.account_id??'').trim(),email=String(b.service_account_email??'').trim(),keyId=String(b.key_id??'').trim(),privateKey=String(b.private_key??'').trim(); if(!/^\d+$/.test(accountId)) return settingsError(c,'Voximplant Account ID must be numeric.'); if(!email.includes('@')||!keyId||!privateKey.includes('BEGIN')) return settingsError(c,'Invalid Voximplant service-account credentials.'); const row=await upsertUserVoximplantCredentials(c.env,c.env.DB,c.get('user').id,{accountId,applicationId:String(b.application_id??''),serviceAccountEmail:email,keyId,privateKey}); return provisionVox(c,row); });
dashboardRoutes.post('/settings/voximplant/reprovision', async (c) => { const row=await c.env.DB.prepare('SELECT * FROM user_voximplant_credentials WHERE user_id=?').bind(c.get('user').id).first<UserVoximplantCredentialRow>(); if(!row) return settingsError(c,'Configure Voximplant credentials first.'); return provisionVox(c,row); });
