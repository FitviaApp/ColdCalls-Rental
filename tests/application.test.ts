import { describe, expect, it, vi } from 'vitest';
import { env, exports } from 'cloudflare:workers';
import { encryptFernet, hashPassword, signToken } from '../src/auth';

const CSRF = '00000000-0000-4000-8000-000000000001';

async function seedUser(id: number, email: string): Promise<string> {
  await env.DB.prepare('INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(?,?,?,0,1,?)')
    .bind(id, email, hashPassword('test-password'), `+1555000000${id}`).run();
  await env.DB.prepare("INSERT OR IGNORE INTO rental_plans(id,code,name,duration_days,price_usdt,is_active) VALUES(1,'daily','Daily',1,15,1)").run();
  await env.DB.prepare("INSERT INTO user_rentals(user_id,plan_id,starts_at,expires_at,status) VALUES(?,1,?,?,'active')")
    .bind(id, new Date().toISOString(), new Date(Date.now() + 86400000).toISOString()).run();
  return signToken(env.JWT_SECRET, id);
}

function request(path: string, token: string, init: RequestInit = {}): Request {
  const headers = new Headers(init.headers);
  headers.set('cookie', `access_token=${token}; csrf_token=${CSRF}`);
  headers.set('accept', 'text/html');
  return new Request(`https://example.test${path}`, { ...init, headers });
}

function form(path: string, token: string, values: Record<string, string>): Request {
  return request(path, token, {
    method: 'POST',
    redirect: 'manual',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ ...values, csrf_token: CSRF }),
  });
}

describe('application persistence and tenant boundaries', () => {
  it('renders the authenticated dashboard with D1-backed data', async () => {
    const token = await seedUser(1, 'one@example.com');
    const response = await exports.default.fetch(request('/dashboard', token));
    expect(response.status).toBe(200);
    expect(await response.text()).toContain('AI Agent Runtime');
  });

  it('renders and validates the Cloudflare AI agent workspace', async () => {
    const token = await seedUser(1, 'one@example.com');

    const emptyResponse = await exports.default.fetch(request('/ai-agents', token));
    expect(emptyResponse.status).toBe(200);
    const emptyHtml = await emptyResponse.text();
    expect(emptyHtml).toContain('No AI agents yet');
    expect(emptyHtml).not.toContain('<table');

    const createPageResponse = await exports.default.fetch(request('/ai-agents/create', token));
    expect(createPageResponse.status).toBe(200);
    const createPageHtml = await createPageResponse.text();
    expect(createPageHtml).toContain('New AI Agent');
    expect(createPageHtml).toContain('value="gpt-realtime-2.1"');
    expect(createPageHtml).toContain('value="0.7"');

    const invalidResponse = await exports.default.fetch(form('/ai-agents/create', token, {
      name: 'Preserve me',
      system_prompt: 'Be helpful.',
      voice_id: 'voice-preserved',
      model: 'gpt-realtime-2.1',
      temperature: '3',
      language: 'pt-BR',
      handoff_description: 'Transfer on request',
      is_active: 'on',
    }));
    expect(invalidResponse.status).toBe(400);
    const invalidHtml = await invalidResponse.text();
    expect(invalidHtml).toContain('Temperature must be between 0.0 and 2.0.');
    expect(invalidHtml).toContain('value="Preserve me"');
    expect(invalidHtml).toContain('value="voice-preserved"');
    expect(invalidHtml).toContain('value="3"');

    await env.DB.prepare(
      "INSERT INTO ai_agents(id,user_id,name,system_prompt,voice_id,model,is_active) VALUES(1,1,'Receptionist','Be helpful.','voice-1','gpt-realtime-2.1',1)",
    ).run();
    const populatedResponse = await exports.default.fetch(request('/ai-agents?toggled=true', token));
    const populatedHtml = await populatedResponse.text();
    expect(populatedHtml).toContain('Receptionist');
    expect(populatedHtml).toContain('<table');
    expect(populatedHtml).toContain('AI agent status updated successfully.');

    await env.DB.batch([
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','primary',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,ai_agent_id,campaign_mode,status) VALUES(1,1,'Live AI',1,1,1,'ai_agent','running')"),
    ]);
    const toggleResponse = await exports.default.fetch(form('/ai-agents/1/toggle', token, {}));
    expect(toggleResponse.status).toBe(400);
    expect(await toggleResponse.json()).toEqual({
      detail: 'Pause the running campaign before disabling this agent',
    });
    expect(
      (await env.DB.prepare('SELECT is_active FROM ai_agents WHERE id=1').first<{ is_active: number }>())
        ?.is_active,
    ).toBe(1);
  });

  it('does not expose another tenant campaign', async () => {
    const token = await seedUser(1, 'one@example.com');
    await seedUser(2, 'two@example.com');
    await env.DB.batch([
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(2,2,'+15550000022','US','tenant two',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(2,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,status,total_numbers) VALUES(2,2,'private',2,2,'draft',0)"),
    ]);
    const response = await exports.default.fetch(request('/campaigns/2', token));
    expect(response.status).toBe(404);
  });

  it('renders campaign progress and number data as valid inline JSON', async () => {
    const token = await seedUser(1, 'one@example.com');
    await env.DB.batch([
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','primary',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,status,total_numbers,processed_numbers,failed_calls) VALUES(1,1,'Status campaign',1,1,'completed',1,1,1)"),
      env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,error_message) VALUES(1,1,'+15550000123','failed','provider rejected <callback> & stopped')"),
    ]);

    const response = await exports.default.fetch(request('/campaigns/1', token));
    expect(response.status).toBe(200);
    const html = await response.text();
    expect(html).toContain("status: 'completed'");
    expect(html).toContain('processed: 1');
    expect(html).toContain('phone_number":"+15550000123"');
    expect(html).not.toContain('numbers: [{&quot;');

    const serialized = html.match(/numbers:\s*(\[.*?\]),\s*polling:/s)?.[1];
    expect(serialized).toBeTruthy();
    expect(JSON.parse(serialized!)).toEqual(expect.arrayContaining([
      expect.objectContaining({ phone_number: '+15550000123', status: 'failed' }),
    ]));
  });

  it('stores audio in R2 and serves it only through an authenticated owner route', async () => {
    const token = await seedUser(1, 'one@example.com');
    await env.AUDIO_R2.put('audios/test.mp3', new Uint8Array([1, 2, 3]), { httpMetadata: { contentType: 'audio/mpeg' } });
    await env.DB.prepare("INSERT INTO audios(id,user_id,name,r2_key,r2_url,is_active) VALUES(1,1,'test','audios/test.mp3','',1)").run();
    const unauthenticated = await exports.default.fetch(new Request('https://example.test/assets/audios/1/content'));
    expect(unauthenticated.status).toBe(401);
    const authenticated = await exports.default.fetch(request('/assets/audios/1/content', token));
    expect(authenticated.status).toBe(200);
    expect([...new Uint8Array(await authenticated.arrayBuffer())]).toEqual([1, 2, 3]);
  });

  it('persists the owner CRUD flow for caller IDs, R2 audio, agents, and campaigns', async () => {
    const token = await seedUser(1, 'one@example.com');
    await env.DB.prepare("INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted,verification_status,verified_at) VALUES(1,?,?,'verified',?)")
      .bind(
        encryptFernet(env.ENCRYPTION_KEY, 'AC00000000000000000000000000000000'),
        encryptFernet(env.ENCRYPTION_KEY, 'test-token'),
        new Date().toISOString(),
      ).run();

    expect((await exports.default.fetch(form('/assets/caller-ids/create', token, {
      phone_number: '+15550000101', country_code: 'us', description: 'Primary',
    }))).status).toBe(302);
    const caller = await env.DB.prepare('SELECT * FROM caller_ids WHERE user_id=1').first<Record<string, any>>();
    expect(caller).toMatchObject({ phone_number: '+15550000101', country_code: 'US', description: 'Primary' });
    expect((await exports.default.fetch(form(`/assets/caller-ids/${caller!.id}/edit`, token, {
      phone_number: '+15550000101', country_code: 'US', description: 'Updated', is_active: 'on',
    }))).status).toBe(302);

    const upload = new FormData();
    upload.set('csrf_token', CSRF);
    upload.set('name', 'Greeting');
    upload.set('file', new File([new Uint8Array([10, 20, 30])], 'greeting.mp3', { type: 'audio/mpeg' }));
    expect((await exports.default.fetch(request('/assets/audios/upload', token, { method: 'POST', redirect: 'manual', body: upload }))).status).toBe(302);
    const audio = await env.DB.prepare('SELECT * FROM audios WHERE user_id=1').first<Record<string, any>>();
    expect(audio?.name).toBe('Greeting');
    expect(await env.AUDIO_R2.get(audio!.r2_key)).not.toBeNull();
    expect((await exports.default.fetch(form(`/assets/audios/${audio!.id}/edit`, token, { name: 'Greeting v2', is_active: 'on' }))).status).toBe(302);

    expect((await exports.default.fetch(form('/ai-agents/create', token, {
      name: 'Receptionist', system_prompt: 'Be concise.', voice_id: 'voice-1', model: 'gpt-realtime-2.1',
      temperature: '0.7', language: 'en', handoff_description: 'Human requested', is_active: 'on',
    }))).status).toBe(302);
    const agent = await env.DB.prepare('SELECT * FROM ai_agents WHERE user_id=1').first<Record<string, any>>();
    expect(agent).toMatchObject({ name: 'Receptionist', is_active: 1 });
    expect((await exports.default.fetch(form(`/ai-agents/${agent!.id}/edit`, token, {
      name: 'Receptionist v2', system_prompt: 'Be helpful.', voice_id: 'voice-2', model: 'gpt-realtime-2.1',
      temperature: '0.5', language: 'en', handoff_description: '', is_active: 'on',
    }))).status).toBe(302);
    expect((await exports.default.fetch(form(`/ai-agents/${agent!.id}/toggle`, token, {}))).status).toBe(302);
    expect((await env.DB.prepare('SELECT is_active FROM ai_agents WHERE id=?').bind(agent!.id).first<{ is_active: number }>())?.is_active).toBe(0);

    expect((await exports.default.fetch(form('/campaigns/create', token, {
      name: 'Audio campaign', campaign_mode: 'audio', voice_provider: 'twilio',
      caller_id_id: String(caller!.id), audio_id: String(audio!.id), max_concurrent_calls: '2',
      numbers_text: '+15550000102\n+15550000103',
    }))).status).toBe(302);
    const campaign = await env.DB.prepare('SELECT * FROM campaigns WHERE user_id=1').first<Record<string, any>>();
    expect(campaign).toMatchObject({ name: 'Audio campaign', total_numbers: 2, status: 'draft' });
    expect((await env.DB.prepare('SELECT COUNT(*) count FROM campaign_numbers WHERE campaign_id=?').bind(campaign!.id).first<{ count: number }>())?.count).toBe(2);
    const twilioFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('IncomingPhoneNumbers.json')) return new Response(JSON.stringify({
        incoming_phone_numbers: [{ sid: 'PN1', phone_number: '+15550000101' }],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
      return new Response(JSON.stringify({
        outgoing_caller_ids: [{ sid: 'PN1', phone_number: '+15550000101' }],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    });
    vi.stubGlobal('fetch', twilioFetch);
    const startResponse = await exports.default.fetch(form(`/campaigns/${campaign!.id}/start`, token, {}));
    expect(twilioFetch.mock.calls[0]?.[0]).toEqual(expect.stringContaining('IncomingPhoneNumbers.json'));
    expect(startResponse.status, await startResponse.clone().text()).toBe(302);
    expect((await exports.default.fetch(form(`/campaigns/${campaign!.id}/pause`, token, {}))).status).toBe(302);
    expect((await exports.default.fetch(form(`/campaigns/${campaign!.id}/cancel`, token, {}))).status).toBe(302);
    expect((await env.DB.prepare('SELECT status FROM campaigns WHERE id=?').bind(campaign!.id).first<{ status: string }>())?.status).toBe('cancelled');
  });

  it('persists admin CRUD while enforcing the admin boundary', async () => {
    await env.DB.prepare('INSERT INTO users(id,email,password_hash,is_admin,is_active) VALUES(1,?,?,1,1)')
      .bind('admin@example.com', hashPassword('admin-test-password')).run();
    const adminToken = await signToken(env.JWT_SECRET, 1);
    const ordinaryToken = await seedUser(2, 'ordinary@example.com');
    expect((await exports.default.fetch(request('/admin', ordinaryToken))).status).toBe(403);

    expect((await exports.default.fetch(form('/admin/countries/create', adminToken, {
      code: 'BR', name: 'Brazil', price_per_minute: '0.05',
    }))).status).toBe(302);
    const country = await env.DB.prepare("SELECT * FROM countries WHERE code='BR'").first<Record<string, any>>();
    expect((await exports.default.fetch(form(`/admin/countries/${country!.id}/edit`, adminToken, {
      code: 'BR', name: 'Brasil', price_per_minute: '0.06', is_active: 'on',
    }))).status).toBe(302);

    expect((await exports.default.fetch(form('/admin/rental-plans/create', adminToken, {
      code: 'monthly', name: 'Monthly', duration_days: '30', price_usdt: '200',
    }))).status).toBe(302);
    const plan = await env.DB.prepare("SELECT * FROM rental_plans WHERE code='monthly'").first<Record<string, any>>();
    expect((await exports.default.fetch(form(`/admin/rental-plans/${plan!.id}/edit`, adminToken, {
      name: 'Monthly Plus', duration_days: '31', price_usdt: '210', is_active: 'on',
    }))).status).toBe(302);

    expect((await exports.default.fetch(form('/admin/users/create', adminToken, {
      email: 'managed@example.com', password: 'managed-password', password_confirm: 'managed-password',
    }))).status).toBe(302);
    const managed = await env.DB.prepare("SELECT * FROM users WHERE email='managed@example.com'").first<Record<string, any>>();
    expect(managed?.is_active).toBe(1);
    expect((await exports.default.fetch(form(`/admin/users/${managed!.id}/toggle`, adminToken, {}))).status).toBe(302);
    expect((await exports.default.fetch(form(`/admin/users/${managed!.id}/add-paid-days`, adminToken, { paid_days: '3' }))).status).toBe(302);
    expect(await env.DB.prepare('SELECT 1 FROM user_rentals WHERE user_id=?').bind(managed!.id).first()).not.toBeNull();
    expect((await exports.default.fetch(form(`/admin/users/${managed!.id}/delete`, adminToken, {}))).status).toBe(302);
    expect(await env.DB.prepare('SELECT 1 FROM users WHERE id=?').bind(managed!.id).first()).toBeNull();
  });
});
