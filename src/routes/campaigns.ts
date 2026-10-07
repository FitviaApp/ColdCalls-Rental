import { Hono, type Context } from 'hono';
import { render } from '../render';
import { type AppEnv, requireRental } from '../middleware';
import { getUserVoiceProviderStatus, hasUserAiRuntimeCredentials, hasUserVoiceProviderCredentials, providerSupportsPress1 } from '../services/user_voice_provider_service';
import { getAiCampaignReadiness } from '../services/ai_campaign_readiness_service';
import { ensureTwilioBasicCampaignReady } from '../services/twilio_basic_readiness';

export const campaignRoutes = new Hono<AppEnv>();
campaignRoutes.use('*', requireRental);

const E164 = /^\+[1-9]\d{1,14}$/;
const enumValue = (value: unknown) => ({ value: String(value ?? '') });
const dateValue = (value: unknown) => ({
  strftime: (format: string) => {
    const date = new Date(String(value ?? ''));
    if (Number.isNaN(date.valueOf())) return '';
    const iso = date.toISOString().replace('T', ' ');
    return format.includes('%H') ? iso.slice(0, 16) : iso.slice(0, 10);
  },
});

function campaignView(row: Record<string, any>): Record<string, any> {
  const progress = row.total_numbers ? (Number(row.processed_numbers) / Number(row.total_numbers)) * 100 : 0;
  return {
    ...row,
    status: enumValue(row.status),
    campaign_mode: enumValue(row.campaign_mode),
    voice_provider: enumValue(row.voice_provider),
    created_at: dateValue(row.created_at),
    progress_percent: Math.min(100, progress),
    ai_agent: row.agent_name ? { name: row.agent_name, model: row.agent_model, voice_id: row.agent_voice_id } : null,
    audio: row.audio_name ? { name: row.audio_name } : null,
    caller_id: { phone_number: row.caller_number },
    country: { name: row.country_name, code: row.country_code, price_per_minute: row.price_per_minute },
    user: { transfer_number: row.transfer_number },
  };
}

async function listDependencies(c: Context<AppEnv>) {
  const userId = c.get('user').id;
  const [callerIds, audios, agents, providers, aiConfigured] = await Promise.all([
    c.env.DB.prepare('SELECT * FROM caller_ids WHERE user_id=? AND is_active=1 ORDER BY phone_number').bind(userId).all(),
    c.env.DB.prepare('SELECT * FROM audios WHERE user_id=? AND is_active=1 ORDER BY name').bind(userId).all(),
    c.env.DB.prepare('SELECT * FROM ai_agents WHERE user_id=? ORDER BY name').bind(userId).all(),
    getUserVoiceProviderStatus(c.env, c.env.DB, userId),
    hasUserAiRuntimeCredentials(c.env, c.env.DB, userId),
  ]);
  return { caller_ids: callerIds.results, audios: audios.results, ai_agents: agents.results, voice_providers: providers, ai_runtime_configured: aiConfigured };
}

campaignRoutes.get('/', async (c) => {
  const rows = await c.env.DB.prepare(
    `SELECT c.*, a.name agent_name, a.model agent_model, a.voice_id agent_voice_id
       FROM campaigns c LEFT JOIN ai_agents a ON a.id=c.ai_agent_id
      WHERE c.user_id=? ORDER BY c.created_at DESC`,
  ).bind(c.get('user').id).all<Record<string, any>>();
  const summaries: Record<number, Record<string, unknown>> = {};
  for (const row of rows.results) {
    if (row.campaign_mode !== 'ai_agent') continue;
    const summary = await c.env.DB.prepare(
      `SELECT COUNT(*) total_numbers,
       SUM(CASE WHEN ai_runtime_error IS NOT NULL THEN 1 ELSE 0 END) runtime_errors,
       SUM(CASE WHEN ai_handoff_reason IS NOT NULL THEN 1 ELSE 0 END) handoffs,
       (SELECT ai_runtime_error FROM campaign_numbers x WHERE x.campaign_id=campaign_numbers.campaign_id AND x.ai_runtime_error IS NOT NULL ORDER BY x.id DESC LIMIT 1) last_runtime_error FROM campaign_numbers WHERE campaign_id=?`,
    ).bind(row.id).first<Record<string, any>>();
    summaries[row.id] = { ...summary, policy_paused: row.status === 'paused' && String(summary?.last_runtime_error ?? '').toLowerCase().includes('policy') };
  }
  return c.html(render('campaigns/list.html', { request: { url: c.req.url }, user: c.get('user'), campaigns: rows.results.map(campaignView), ai_runtime_summary_by_campaign: summaries }));
});

campaignRoutes.get('/create', async (c) => {
  const deps = await listDependencies(c);
  const error = !c.get('user').transfer_number ? 'Please configure your Transfer Number (3CX) in Settings before creating a campaign.' : null;
  return c.html(render('campaigns/create.html', { request: { url: c.req.url }, user: c.get('user'), ...deps, error, form_data: {} }));
});

campaignRoutes.post('/create', async (c) => {
  const body = await c.req.parseBody();
  const user = c.get('user');
  const mode = String(body.campaign_mode ?? 'audio').trim();
  const provider = String(body.voice_provider ?? 'twilio').trim().toLowerCase();
  const callerId = Number(body.caller_id_id);
  const audioId = String(body.audio_id ?? '').trim() ? Number(body.audio_id) : null;
  const agentId = String(body.ai_agent_id ?? '').trim() ? Number(body.ai_agent_id) : null;
  const concurrency = Math.trunc(Number(body.max_concurrent_calls ?? 1));
  const press1 = String(body.press_1_to_talk_with_agent ?? '') === 'on';
  let rawNumbers = String(body.numbers_text ?? '');
  if (body.numbers_file instanceof File && body.numbers_file.size) rawNumbers = await body.numbers_file.text();
  const numbers = [...new Set(rawNumbers.split(/[\s,;]+/).map((v) => v.trim()).filter((v) => E164.test(v)))];

  let error = '';
  if (!user.transfer_number) error = 'Please configure your Transfer Number (3CX) in Settings before creating a campaign.';
  else if (!['audio', 'ai_agent'].includes(mode)) error = 'Invalid campaign mode selected.';
  else if (!(await hasUserVoiceProviderCredentials(c.env, c.env.DB, user.id, provider))) error = `Please configure ${provider} credentials in Settings first.`;
  else if (press1 && !providerSupportsPress1(provider)) error = 'Press 1 is not supported by the selected provider.';
  else if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 20) error = 'Concurrent calls must be between 1 and 20.';
  else if (!numbers.length) error = 'No valid phone numbers found. Numbers must use E.164 format.';
  else if (numbers.length > 5000) error = 'Too many numbers (max 5000 per campaign).';
  else if (mode === 'ai_agent' && provider !== 'twilio') error = 'AI agent campaigns currently require Twilio as the voice provider.';

  const caller = await c.env.DB.prepare('SELECT * FROM caller_ids WHERE id=? AND user_id=? AND is_active=1').bind(callerId, user.id).first<Record<string, any>>();
  const audio = audioId ? await c.env.DB.prepare('SELECT id FROM audios WHERE id=? AND user_id=? AND is_active=1').bind(audioId, user.id).first() : null;
  const agent = agentId ? await c.env.DB.prepare('SELECT * FROM ai_agents WHERE id=? AND user_id=? AND is_active=1').bind(agentId, user.id).first<Record<string, any>>() : null;
  if (!caller) error ||= 'Invalid Caller ID selection.';
  if (mode === 'audio' && audioId && !audio) error ||= 'Invalid audio selection.';
  if (mode === 'ai_agent') {
    if (!agent) error ||= 'Select an active AI agent.';
    else error ||= (await getAiCampaignReadiness(c.env, { userId: user.id, voiceProvider: provider, aiAgent: agent })).error ?? '';
  }
  if (error) {
    const deps = await listDependencies(c);
    return c.html(render('campaigns/create.html', { request: { url: c.req.url }, user, ...deps, error, form_data: Object.fromEntries(Object.entries(body).filter(([, v]) => typeof v === 'string')) }), 400);
  }

  const countryCode = String(caller!.country_code).toUpperCase().slice(0, 5);
  await c.env.DB.prepare('INSERT OR IGNORE INTO countries(code,name,price_per_minute,is_active) VALUES(?,?,0,1)').bind(countryCode, countryCode).run();
  const country = await c.env.DB.prepare('SELECT id FROM countries WHERE code=?').bind(countryCode).first<{ id: number }>();
  const inserted = await c.env.DB.batch([
    c.env.DB.prepare(
      `INSERT INTO campaigns(user_id,name,caller_id_id,country_id,audio_id,ai_agent_id,campaign_mode,status,
        press_1_to_talk_with_agent,voice_provider,max_concurrent_calls,total_numbers)
       VALUES(?,?,?,?,?,?,?,'draft',?,?,?,?) RETURNING id`,
    ).bind(user.id, String(body.name ?? '').trim(), callerId, country!.id, mode === 'audio' ? audioId : null,
      mode === 'ai_agent' ? agentId : null, mode, press1 ? 1 : 0, provider, concurrency, numbers.length),
    ...numbers.map((number) => c.env.DB.prepare(
      "INSERT INTO campaign_numbers(campaign_id,phone_number,status) SELECT id,?,'pending' FROM campaigns WHERE user_id=? AND status='draft' ORDER BY id DESC LIMIT 1",
    ).bind(number, user.id)),
  ]);
  const campaignId = (inserted[0] as { results: { id: number }[] }).results[0].id;
  return c.redirect(`/campaigns/${campaignId}`, 302);
});

campaignRoutes.get('/:id', async (c) => {
  const id = Number(c.req.param('id'));
  const row = await c.env.DB.prepare(
    `SELECT c.*, ci.phone_number caller_number, co.name country_name, co.code country_code,
      co.price_per_minute, au.name audio_name, a.name agent_name, a.model agent_model,
      a.voice_id agent_voice_id, u.transfer_number
     FROM campaigns c JOIN caller_ids ci ON ci.id=c.caller_id_id JOIN countries co ON co.id=c.country_id
     JOIN users u ON u.id=c.user_id LEFT JOIN audios au ON au.id=c.audio_id LEFT JOIN ai_agents a ON a.id=c.ai_agent_id
     WHERE c.id=? AND c.user_id=?`,
  ).bind(id, c.get('user').id).first<Record<string, any>>();
  if (!row) return c.json({ detail: 'Campaign not found' }, 404);
  const result = await c.env.DB.prepare('SELECT * FROM campaign_numbers WHERE campaign_id=? ORDER BY id').bind(id).all<Record<string, any>>();
  const numbers = result.results;
  const aiSummary = row.campaign_mode === 'ai_agent' ? (() => {
    const turnCounts = numbers.map((n) => n.ai_turn_count).filter((v) => v !== null && v !== undefined);
    return {
      total_numbers: numbers.length,
      handoffs: numbers.filter((n) => n.ai_handoff_reason).length,
      runtime_errors: numbers.filter((n) => n.ai_runtime_error).length,
      lead_reprompts: numbers.reduce((sum, n) => sum + Number(n.ai_no_input_turns ?? 0), 0),
      avg_turns: turnCounts.length ? turnCounts.reduce((sum, v) => sum + Number(v), 0) / turnCounts.length : 0,
      last_handoff_reason: [...numbers].reverse().find((n: Record<string, any>) => n.ai_handoff_reason)?.ai_handoff_reason ?? null,
      last_runtime_error: [...numbers].reverse().find((n: Record<string, any>) => n.ai_runtime_error)?.ai_runtime_error ?? null,
      auto_pause_reason: row.status === 'paused'
        ? [...numbers].reverse().find((n: Record<string, any>) => n.ai_runtime_error)?.ai_runtime_error ?? null
        : null,
      policy_paused: row.status === 'paused' && numbers.some((n) => String(n.ai_runtime_error ?? '').toLowerCase().includes('policy')),
    };
  })() : null;
  const payload = numbers.map((n) => ({ ...n, status: n.status }));
  return c.html(render('campaigns/detail.html', { request: { url: c.req.url }, user: c.get('user'), campaign: campaignView(row), numbers, numbers_payload: payload, ai_runtime_summary: aiSummary }));
});

campaignRoutes.post('/:id/start', async (c) => changeStatus(c, 'running'));
campaignRoutes.post('/:id/pause', async (c) => changeStatus(c, 'paused'));
campaignRoutes.post('/:id/cancel', async (c) => changeStatus(c, 'cancelled'));

async function changeStatus(c: Context<AppEnv>, target: 'running' | 'paused' | 'cancelled') {
  const id = Number(c.req.param('id'));
  const campaign = await c.env.DB.prepare('SELECT * FROM campaigns WHERE id=? AND user_id=?').bind(id, c.get('user').id).first<Record<string, any>>();
  if (!campaign) return c.json({ detail: 'Campaign not found' }, 404);
  if (target === 'running' && !['draft', 'paused'].includes(campaign.status)) return c.json({ detail: 'Campaign cannot be started' }, 400);
  if (target === 'paused' && campaign.status !== 'running') return c.json({ detail: 'Campaign is not running' }, 400);
  if (target === 'cancelled' && campaign.status === 'completed') return c.json({ detail: 'Campaign is already completed' }, 400);
  if (target === 'running') {
    if (!c.get('user').transfer_number) return c.json({ detail: 'Please configure your Transfer Number (3CX) in Settings first' }, 400);
    if (!(await hasUserVoiceProviderCredentials(c.env, c.env.DB, c.get('user').id, campaign.voice_provider))) {
      return c.json({ detail: `Please configure ${campaign.voice_provider} credentials in Settings first` }, 400);
    }
    if (campaign.voice_provider === 'twilio' && campaign.campaign_mode === 'audio') {
      try {
        await ensureTwilioBasicCampaignReady(c.env, id, c.get('user').id);
      } catch (error) {
        return c.json({ detail: error instanceof Error ? error.message : 'Twilio readiness validation failed' }, 400);
      }
    }
    if (campaign.campaign_mode === 'ai_agent') {
      const agent = await c.env.DB.prepare('SELECT * FROM ai_agents WHERE id=? AND user_id=?').bind(campaign.ai_agent_id, c.get('user').id).first<Record<string, any>>();
      const readiness = await getAiCampaignReadiness(c.env, { userId: c.get('user').id, voiceProvider: campaign.voice_provider, aiAgent: agent });
      if (!readiness.ok) return c.json({ detail: readiness.error }, 400);
    }
  }
  await c.env.DB.prepare(`UPDATE campaigns SET status=?, started_at=CASE WHEN ?='running' THEN COALESCE(started_at,?) ELSE started_at END, completed_at=CASE WHEN ?='cancelled' THEN ? ELSE completed_at END WHERE id=?`)
    .bind(target, target, new Date().toISOString(), target, new Date().toISOString(), id).run();
  if (target === 'cancelled') {
    await c.env.DB.prepare("UPDATE campaign_numbers SET status='cancelled',dispatch_state='finished',processed_at=?,updated_at=? WHERE campaign_id=? AND status IN ('pending','claimed')")
      .bind(new Date().toISOString(), new Date().toISOString(), id).run();
  }
  const stub = c.env.CAMPAIGN_COORDINATORS.get(c.env.CAMPAIGN_COORDINATORS.idFromName(String(id))) as unknown as {
    start(id: number): Promise<void>; pause(): Promise<void>; cancel(): Promise<void>;
  };
  if (target === 'running') await stub.start(id);
  else if (target === 'paused') await stub.pause();
  else await stub.cancel();
  return c.redirect(`/campaigns/${id}`, 302);
}
