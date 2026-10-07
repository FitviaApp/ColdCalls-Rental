import { afterEach, describe, expect, it, vi } from 'vitest';
import { env } from 'cloudflare:workers';
import { runDurableObjectAlarm, runInDurableObject } from 'cloudflare:test';
import { encryptFernet } from '../src/auth';
import {
  SIGNALWIRE_MAX_START_INTERVAL_MS,
  SIGNALWIRE_MIN_START_INTERVAL_MS,
  SIGNALWIRE_CALLBACK_WAIT_MS,
  TWILIO_START_INTERVAL_MS,
} from '../src/durable/campaign_coordinator';

async function seedCampaign(id: number): Promise<DurableObjectStub<import('../src/index').CampaignCoordinator>> {
  const key = env.ENCRYPTION_KEY;
  await env.DB.batch([
    env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(?,?, 'x',0,1,'+15550000003')").bind(id, `owner-${id}@example.com`),
    env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(?,?,?,'US','test',1)").bind(id, id, `+1555${String(id).padStart(7, '0')}`),
    env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
    env.DB.prepare("INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted,verification_status,verified_at) VALUES(?,?,?,'verified',?)")
      .bind(id, encryptFernet(key, 'AC00000000000000000000000000000000'), encryptFernet(key, 'token'), new Date().toISOString()),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,campaign_mode,status,voice_provider,max_concurrent_calls,total_numbers) VALUES(?,?, 'test',?,1,'audio','running','twilio',2,1)").bind(id, id, id),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status) VALUES(?,?, '+15550000002','pending')").bind(id, id),
  ]);
  return env.CAMPAIGN_COORDINATORS.get(env.CAMPAIGN_COORDINATORS.idFromName(String(id)));
}

async function seedSignalWireCampaigns(): Promise<[
  DurableObjectStub<import('../src/index').CampaignCoordinator>,
  DurableObjectStub<import('../src/index').CampaignCoordinator>,
]> {
  const key = env.ENCRYPTION_KEY;
  await env.DB.batch([
    env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
    env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
    env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
    env.DB.prepare('INSERT INTO user_signalwire_credentials(user_id,project_id_encrypted,api_token_encrypted,space_url_encrypted) VALUES(1,?,?,?)')
      .bind(encryptFernet(key, 'project'), encryptFernet(key, 'token'), encryptFernet(key, 'space.signalwire.com')),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,campaign_mode,status,voice_provider,max_concurrent_calls,total_numbers) VALUES(201,1,'first',1,1,'audio','running','signalwire',1,1)"),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,campaign_mode,status,voice_provider,max_concurrent_calls,total_numbers) VALUES(202,1,'second',1,1,'audio','running','signalwire',1,1)"),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status) VALUES(201,201,'+15550000002','pending')"),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status) VALUES(202,202,'+15550000004','pending')"),
  ]);
  return [201, 202].map((id) => env.CAMPAIGN_COORDINATORS.get(
    env.CAMPAIGN_COORDINATORS.idFromName(String(id)),
  )) as [
    DurableObjectStub<import('../src/index').CampaignCoordinator>,
    DurableObjectStub<import('../src/index').CampaignCoordinator>,
  ];
}

async function seedSignalWireCampaign(id: number): Promise<DurableObjectStub<import('../src/index').CampaignCoordinator>> {
  const key = env.ENCRYPTION_KEY;
  await env.DB.batch([
    env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(?,?, 'x',0,1,'+15550000003')")
      .bind(id, `signalwire-${id}@example.com`),
    env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(?,?,?,'US','test',1)")
      .bind(id, id, `+1555${String(id).padStart(7, '0')}`),
    env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
    env.DB.prepare('INSERT INTO user_signalwire_credentials(user_id,project_id_encrypted,api_token_encrypted,space_url_encrypted) VALUES(?,?,?,?)')
      .bind(id, encryptFernet(key, 'project'), encryptFernet(key, 'token'), encryptFernet(key, 'space.signalwire.com')),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,campaign_mode,status,voice_provider,max_concurrent_calls,total_numbers) VALUES(?,?, 'callback-wait',?,1,'audio','running','signalwire',1,1)")
      .bind(id, id, id),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status) VALUES(?,?, '+15550000002','pending')")
      .bind(id, id),
  ]);
  return env.CAMPAIGN_COORDINATORS.get(env.CAMPAIGN_COORDINATORS.idFromName(String(id)));
}

async function seedTwilioCampaigns(): Promise<[
  DurableObjectStub<import('../src/index').CampaignCoordinator>,
  DurableObjectStub<import('../src/index').CampaignCoordinator>,
]> {
  const key = env.ENCRYPTION_KEY;
  await env.DB.batch([
    env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(999,'twilio-rate@example.com','x',0,1,'+15550000003')"),
    env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(999,999,'+15550000001','US','test',1)"),
    env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
    env.DB.prepare("INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted,verification_status,verified_at) VALUES(999,?,?,'verified',?)")
      .bind(encryptFernet(key, 'AC00000000000000000000000000000000'), encryptFernet(key, 'token'), new Date().toISOString()),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,campaign_mode,status,voice_provider,max_concurrent_calls,total_numbers) VALUES(301,999,'first',999,1,'audio','running','twilio',1,1)"),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,campaign_mode,status,voice_provider,max_concurrent_calls,total_numbers) VALUES(302,999,'second',999,1,'audio','running','twilio',1,1)"),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status) VALUES(301,301,'+15550000002','pending')"),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status) VALUES(302,302,'+15550000004','pending')"),
  ]);
  return [301, 302].map((id) => env.CAMPAIGN_COORDINATORS.get(
    env.CAMPAIGN_COORDINATORS.idFromName(String(id)),
  )) as [
    DurableObjectStub<import('../src/index').CampaignCoordinator>,
    DurableObjectStub<import('../src/index').CampaignCoordinator>,
  ];
}

async function seedTwilioCostCampaign(id: number, attempts: number): Promise<DurableObjectStub<import('../src/index').CampaignCoordinator>> {
  const key = env.ENCRYPTION_KEY;
  await env.DB.batch([
    env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(?,?, 'x',0,1,'+15550000003')").bind(id, `cost-${id}@example.com`),
    env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(?,?, '+15550000001','US','test',1)").bind(id, id),
    env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
    env.DB.prepare("INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted,verification_status,verified_at) VALUES(?,?,?,'verified',?)")
      .bind(id, encryptFernet(key, 'AC00000000000000000000000000000000'), encryptFernet(key, 'token'), new Date().toISOString()),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,campaign_mode,status,voice_provider,max_concurrent_calls,total_numbers) VALUES(?,?, 'cost',?,1,'audio','completed','twilio',1,1)").bind(id, id, id),
    env.DB.prepare(`INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,call_sid,transfer_call_sid,
      cost_reconcile_status,cost_reconcile_attempts,cost_next_retry_at)
      VALUES(?,?, '+15550000002','completed','CA-PARENT','CA-CHILD','pending',?,?)`)
      .bind(id, id, attempts, new Date(Date.now() - 1_000).toISOString()),
  ]);
  return env.CAMPAIGN_COORDINATORS.get(env.CAMPAIGN_COORDINATORS.idFromName(String(id)));
}

afterEach(() => vi.unstubAllGlobals());

describe('CampaignCoordinator dispatch safety', () => {
  it('marks an ambiguous provider result and never redials it', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('connection reset after write'));
    vi.stubGlobal('fetch', fetchMock);
    const stub = await seedCampaign(101);
    await stub.start(101);
    await runInDurableObject(stub, (instance) => instance.alarm());
    const row = await env.DB.prepare('SELECT status,dispatch_state,dispatch_attempts FROM campaign_numbers WHERE id=101').first<Record<string, any>>();
    expect(row).toMatchObject({ status: 'dispatch_unknown', dispatch_state: 'dispatch_unknown', dispatch_attempts: 1 });
    await stub.reconcile(101);
    await runInDurableObject(stub, (instance) => instance.alarm());
    const after = await env.DB.prepare('SELECT dispatch_attempts FROM campaign_numbers WHERE id=101').first<{ dispatch_attempts: number }>();
    expect(after?.dispatch_attempts).toBe(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('classifies an explicit provider 4xx as a known failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ message: 'invalid destination', code: 21211 }), { status: 400, headers: { 'content-type': 'application/json' } })));
    const stub = await seedCampaign(102);
    await stub.start(102);
    await runInDurableObject(stub, (instance) => instance.alarm());
    const row = await env.DB.prepare('SELECT status,dispatch_attempts FROM campaign_numbers WHERE id=102').first<Record<string, any>>();
    expect(row).toMatchObject({ status: 'failed', dispatch_attempts: 1 });
  });

  it('removes the recovery alarm when paused', async () => {
    const stub = await seedCampaign(103);
    await stub.start(103);
    await stub.pause();
    expect(await runDurableObjectAlarm(stub)).toBe(false);
  });

  it('recovers a stale claim as dispatch_unknown without calling the provider', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const stub = await seedCampaign(104);
    await env.DB.prepare("UPDATE campaign_numbers SET status='claimed',dispatch_state='claimed',claimed_at=? WHERE id=104")
      .bind(new Date(Date.now() - 180_000).toISOString()).run();
    await stub.start(104);
    await runInDurableObject(stub, (instance) => instance.alarm());
    const row = await env.DB.prepare('SELECT status,dispatch_state FROM campaign_numbers WHERE id=104').first<Record<string, any>>();
    expect(row).toMatchObject({ status: 'dispatch_unknown', dispatch_state: 'dispatch_unknown' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('persists a SignalWire provider slot for 3 to 5 seconds and rejects duplicate reservations', async () => {
    const limiter = env.CAMPAIGN_COORDINATORS.get(
      env.CAMPAIGN_COORDINATORS.idFromName('provider-rate:test-user:signalwire'),
    );
    const before = Date.now();
    const first = await limiter.reserveProviderSlot(
      SIGNALWIRE_MIN_START_INTERVAL_MS,
      SIGNALWIRE_MAX_START_INTERVAL_MS,
    );
    expect(first.granted).toBe(true);
    expect(first.retryAt - before).toBeGreaterThanOrEqual(SIGNALWIRE_MIN_START_INTERVAL_MS);
    expect(first.retryAt - before).toBeLessThanOrEqual(SIGNALWIRE_MAX_START_INTERVAL_MS + 100);

    const duplicate = await limiter.reserveProviderSlot(
      SIGNALWIRE_MIN_START_INTERVAL_MS,
      SIGNALWIRE_MAX_START_INTERVAL_MS,
    );
    expect(duplicate).toEqual({ granted: false, retryAt: first.retryAt });
  });

  it('discards a superseded long cooldown and restores the 3 to 5 second policy', async () => {
    const limiter = env.CAMPAIGN_COORDINATORS.get(
      env.CAMPAIGN_COORDINATORS.idFromName('provider-rate:legacy-cooldown:signalwire'),
    );
    expect((await limiter.reserveProviderSlot(3_600_000, 3_600_000)).granted).toBe(true);

    const before = Date.now();
    const restored = await limiter.reserveProviderSlot(
      SIGNALWIRE_MIN_START_INTERVAL_MS,
      SIGNALWIRE_MAX_START_INTERVAL_MS,
    );
    expect(restored.granted).toBe(true);
    expect(restored.retryAt - before).toBeGreaterThanOrEqual(SIGNALWIRE_MIN_START_INTERVAL_MS);
    expect(restored.retryAt - before).toBeLessThanOrEqual(SIGNALWIRE_MAX_START_INTERVAL_MS + 100);
  });

  it('shares the SignalWire start-rate gate across campaigns without claiming the waiting number', async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ sid: 'SW201', status: 'queued' }), { status: 201 }));
    vi.stubGlobal('fetch', fetchMock);
    const [first, second] = await seedSignalWireCampaigns();
    await first.start(201);
    await second.start(202);

    await runInDurableObject(first, (instance) => instance.alarm());
    await runInDurableObject(second, (instance) => instance.alarm());

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(await env.DB.prepare('SELECT status,dispatch_attempts,error_message FROM campaign_numbers WHERE id=201').first()).toMatchObject({
      status: 'calling', dispatch_attempts: 1,
      error_message: null,
    });
    expect(await env.DB.prepare('SELECT status,dispatch_attempts FROM campaign_numbers WHERE id=202').first()).toMatchObject({
      status: 'pending', dispatch_attempts: 0,
    });
  });

  it('waits 120 seconds for a SignalWire callback after an empty 2xx and never redials', async () => {
    vi.spyOn(console, 'info').mockImplementation(() => undefined);
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);
    const first = await seedSignalWireCampaign(250);
    await first.start(250);
    const before = Date.now();
    await runInDurableObject(first, (instance) => instance.alarm());
    const waiting = await env.DB.prepare(
      'SELECT status,dispatch_state,dispatch_attempts,call_sid,next_action_at FROM campaign_numbers WHERE id=250',
    ).first<Record<string, any>>();
    expect(waiting).toMatchObject({
      status: 'calling', dispatch_state: 'awaiting_callback', dispatch_attempts: 1, call_sid: null,
    });
    expect(new Date(String(waiting?.next_action_at)).valueOf() - before).toBeGreaterThanOrEqual(
      SIGNALWIRE_CALLBACK_WAIT_MS - 1_000,
    );

    await runInDurableObject(first, (instance) => instance.alarm());
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await env.DB.prepare('UPDATE campaign_numbers SET next_action_at=? WHERE id=250')
      .bind(new Date(Date.now() - 1_000).toISOString()).run();
    await runInDurableObject(first, (instance) => instance.alarm());
    expect(await env.DB.prepare(
      'SELECT status,dispatch_state,dispatch_attempts,error_message FROM campaign_numbers WHERE id=250',
    ).first()).toMatchObject({
      status: 'dispatch_unknown', dispatch_state: 'dispatch_unknown', dispatch_attempts: 1,
    });
    expect(await env.DB.prepare('SELECT status,failed_calls FROM campaigns WHERE id=250').first()).toMatchObject({
      status: 'completed', failed_calls: 1,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps SignalWire rate gates independent between users', async () => {
    const first = env.CAMPAIGN_COORDINATORS.get(
      env.CAMPAIGN_COORDINATORS.idFromName('provider-rate:user-a:signalwire'),
    );
    const second = env.CAMPAIGN_COORDINATORS.get(
      env.CAMPAIGN_COORDINATORS.idFromName('provider-rate:user-b:signalwire'),
    );
    expect((await first.reserveProviderSlot(3000, 5000)).granted).toBe(true);
    expect((await second.reserveProviderSlot(3000, 5000)).granted).toBe(true);
  });

  it('shares the fixed one-per-second Twilio start gate across campaigns', async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ sid: 'CA301', status: 'queued' }), { status: 201 }));
    vi.stubGlobal('fetch', fetchMock);
    const [first, second] = await seedTwilioCampaigns();
    await first.start(301);
    await second.start(302);
    await runInDurableObject(first, (instance) => instance.alarm());
    await runInDurableObject(second, (instance) => instance.alarm());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const firstRow = await env.DB.prepare('SELECT status,dispatch_attempts,error_message FROM campaign_numbers WHERE id=301').first();
    expect(firstRow, JSON.stringify(firstRow)).toMatchObject({ status: 'calling', dispatch_attempts: 1 });
    expect(await env.DB.prepare('SELECT status,dispatch_attempts FROM campaign_numbers WHERE id=302').first()).toMatchObject({ status: 'pending', dispatch_attempts: 0 });
    const limiter = env.CAMPAIGN_COORDINATORS.get(env.CAMPAIGN_COORDINATORS.idFromName('provider-rate:999:twilio'));
    const reservation = await limiter.reserveProviderSlot(TWILIO_START_INTERVAL_MS, TWILIO_START_INTERVAL_MS);
    expect(reservation.granted).toBe(false);
  });

  it('reconciles parent and transfer prices into the campaign total', async () => {
    const stub = await seedTwilioCostCampaign(401, 0);
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const price = String(input).includes('CA-PARENT') ? '-0.012' : '-0.034';
      return new Response(JSON.stringify({ sid: String(input).includes('CA-PARENT') ? 'CA-PARENT' : 'CA-CHILD', status: 'completed', duration: '5', price }), { status: 200 });
    }));
    await stub.reconcile(401);
    await runInDurableObject(stub, (instance) => instance.alarm());
    expect(await env.DB.prepare('SELECT cost,cost_reconcile_status,cost_reconciled_at FROM campaign_numbers WHERE id=401').first()).toMatchObject({
      cost: 0.046, cost_reconcile_status: 'reconciled',
    });
    expect(await env.DB.prepare('SELECT total_cost FROM campaigns WHERE id=401').first()).toMatchObject({ total_cost: 0.046 });
  });

  it('marks Twilio cost unconfirmed after the sixth unavailable readback', async () => {
    const stub = await seedTwilioCostCampaign(402, 5);
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      sid: 'CA-PARENT', status: 'completed', duration: '5', price: null,
    }), { status: 200 })));
    await stub.reconcile(402);
    await runInDurableObject(stub, (instance) => instance.alarm());
    expect(await env.DB.prepare('SELECT cost_reconcile_status,cost_reconcile_attempts,cost_next_retry_at FROM campaign_numbers WHERE id=402').first()).toMatchObject({
      cost_reconcile_status: 'unconfirmed', cost_reconcile_attempts: 6, cost_next_retry_at: null,
    });
  });

});
