import { describe, expect, it } from 'vitest';
import { env, exports } from 'cloudflare:workers';
import { encryptFernet } from '../src/auth';
import { verifyIllyvoipWebhook, IllyvoipWebhookError } from '../src/services/illyvoip_webhook';

const WEBHOOK_SECRET = env.ILLYVOIP_WEBHOOK_SECRET || 'illyvoip-webhook-secret';

async function signPayload(payload: Record<string, unknown>, timestamp = Math.floor(Date.now() / 1000)): Promise<{ signature: string; raw: string }> {
  const raw = JSON.stringify(payload);
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(WEBHOOK_SECRET),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const digest = new Uint8Array(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`${timestamp}.${raw}`)));
  const hex = Array.from(digest).map((b) => b.toString(16).padStart(2, '0')).join('');
  return { signature: `t=${timestamp},v1=${hex}`, raw };
}

async function seedIllyvoipCall(callSid: string): Promise<void> {
  await env.DB.batch([
    env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
    env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
    env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
    env.DB.prepare("INSERT INTO audios(id,user_id,name,r2_key,r2_url,is_active) VALUES(1,1,'prompt','audio-key','',1)"),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,audio_id,status,voice_provider,press_1_to_talk_with_agent,total_numbers) VALUES(7,1,'illy',1,1,1,'running','illyvoip',0,1)"),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,call_sid,dispatch_attempt_id) VALUES(11,7,'+15550000002','calling',?,'attempt-11')").bind(callSid),
    env.DB.prepare('INSERT INTO user_illyvoip_credentials(user_id,api_key_encrypted) VALUES(1,?)').bind(encryptFernet(env.ENCRYPTION_KEY, 'illy-key')),
  ]);
}

describe('illyvoip webhook signature verification', () => {
  it('accepts a correctly signed event', async () => {
    const now = Math.floor(Date.now() / 1000);
    const payload = { id: 'evt_1', type: 'call_answered', data: { call: { call_control_id: 'vapi_11' } } };
    const { signature, raw } = await signPayload(payload, now);
    const event = await verifyIllyvoipWebhook(raw, signature, WEBHOOK_SECRET, now);
    expect(event).toMatchObject({ id: 'evt_1', type: 'call_answered' });
  });

  it('rejects a bad signature', async () => {
    const now = Math.floor(Date.now() / 1000);
    const payload = { id: 'evt_1', type: 'call_answered', data: { call: { call_control_id: 'vapi_11' } } };
    const { raw } = await signPayload(payload, now);
    await expect(verifyIllyvoipWebhook(raw, `t=${now},v1=` + 'a'.repeat(64), WEBHOOK_SECRET, now))
      .rejects.toBeInstanceOf(IllyvoipWebhookError);
  });

  it('rejects events with no secret configured', async () => {
    const now = Math.floor(Date.now() / 1000);
    const payload = { id: 'evt_1', type: 'call_answered' };
    const { signature, raw } = await signPayload(payload, now);
    await expect(verifyIllyvoipWebhook(raw, signature, '', now))
      .rejects.toMatchObject({ status: 503 });
  });
});

describe('illyvoip webhook route', () => {
  it('records the destination answer from a call_answered webhook', async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,NULL)"),
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,audio_id,status,voice_provider,press_1_to_talk_with_agent,total_numbers) VALUES(7,1,'illy',1,1,NULL,'running','illyvoip',0,1)"),
      env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,call_sid,dispatch_attempt_id) VALUES(11,7,'+15550000002','calling','vapi_11','attempt-11')"),
      env.DB.prepare('INSERT INTO user_illyvoip_credentials(user_id,api_key_encrypted) VALUES(1,?)').bind(encryptFernet(env.ENCRYPTION_KEY, 'illy-key')),
    ]);

    const payload = {
      id: 'evt_answer_11',
      type: 'call_answered',
      data: { call: { call_id: 'vapi_11', voice_api_control: false, answered_at: '2026-10-21T12:37:24+00:00' } },
    };
    const { signature, raw } = await signPayload(payload);
    const response = await exports.default.fetch(new Request('https://example.test/api/illyvoip/webhook', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'Illyvoip-Signature': signature },
      body: raw,
    }));
    expect(response.status).toBe(200);
    const row = await env.DB.prepare('SELECT provider_sequence,destination_answered_at FROM campaign_numbers WHERE id=11').first();
    expect(row).toMatchObject({ provider_sequence: -1, destination_answered_at: expect.any(String) });
  });

  it('matches a call report answer to the in-flight call by phone numbers', async () => {
    await seedIllyvoipCall('vapi_13');
    const payload = {
      id: 'evt_report_13',
      type: 'call_answered',
      data: {
        call: {
          call_id: '1792586210.287877', direction: 'outgoing',
          from: '15550000001', to: '15550000002', dialed: '15550000002',
          answered_at: '2026-10-21T12:37:24+00:00', voice_api_control: false,
        },
      },
    };
    const { signature, raw } = await signPayload(payload);
    const response = await exports.default.fetch(new Request('https://example.test/api/illyvoip/webhook', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'Illyvoip-Signature': signature },
      body: raw,
    }));
    expect(response.status).toBe(200);
    const row = await env.DB.prepare('SELECT status,destination_answered_at FROM campaign_numbers WHERE id=11').first();
    expect(row).toMatchObject({ status: 'calling', destination_answered_at: '2026-10-21T12:37:24+00:00' });
  });

  it('finalizes the call on call_completed and ignores duplicates', async () => {
    await seedIllyvoipCall('vapi_12');
    const payload = { id: 'evt_complete_12', type: 'call_completed', data: { call: { call_control_id: 'vapi_12', state: 'completed', duration: 42 } } };
    const { signature, raw } = await signPayload(payload);
    const post = () => exports.default.fetch(new Request('https://example.test/api/illyvoip/webhook', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'Illyvoip-Signature': signature },
      body: raw,
    }));

    const first = await post();
    expect(first.status).toBe(200);
    const row = await env.DB.prepare('SELECT status,provider_status,duration_seconds FROM campaign_numbers WHERE id=11').first();
    expect(row).toMatchObject({ status: 'completed', provider_status: 'completed', duration_seconds: 42 });

    const second = await post();
    expect(await second.json()).toMatchObject({ ok: true, duplicate: true });
  });

});
