import { describe, expect, it } from 'vitest';
import { env, exports } from 'cloudflare:workers';
import { encryptFernet } from '../src/auth';
import { createProviderCallbackToken } from '../src/services/provider_callback_token';

async function twilioSignature(url: string, body: URLSearchParams, token: string): Promise<string> {
  let payload = url;
  for (const key of [...new Set(body.keys())].sort()) {
    for (const value of body.getAll(key)) payload += key + value;
  }
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(token), { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
  const digest = new Uint8Array(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(payload)));
  let binary = '';
  for (const byte of digest) binary += String.fromCharCode(byte);
  return btoa(binary);
}

describe('provider callback authentication', () => {
  it('serves press-1 LaML and private R2 audio through scoped signed URLs', async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO audios(id,user_id,name,r2_key,r2_url,is_active) VALUES(1,1,'intro','audios/intro.mp3','',1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,audio_id,status,voice_provider,total_numbers,press_1_to_talk_with_agent) VALUES(1,1,'audio',1,1,1,'running','signalwire',1,1)"),
    ]);
    await env.AUDIO_R2.put('audios/intro.mp3', new Uint8Array([73, 68, 51]), {
      httpMetadata: { contentType: 'audio/mpeg' },
    });

    const token = await createProviderCallbackToken(env, 'signalwire', 1);
    const response = await exports.default.fetch(new Request(`https://example.test/api/twiml/1?token=${encodeURIComponent(token)}`));
    expect(response.status).toBe(200);
    const laml = await response.text();
    expect(laml).toContain('<Play>https://example.test/api/media/audio/1?token=');
    expect(laml).toContain('/api/twiml/1/gather?token=');

    const encodedMediaUrl = laml.match(/<Play>(.*?)<\/Play>/)?.[1];
    expect(encodedMediaUrl).toBeTruthy();
    const mediaUrl = encodedMediaUrl!.replace(/&amp;/g, '&');
    const media = await exports.default.fetch(new Request(mediaUrl));
    expect(media.status).toBe(200);
    expect(media.headers.get('content-type')).toBe('audio/mpeg');
    expect([...new Uint8Array(await media.arrayBuffer())]).toEqual([73, 68, 51]);
  });

  it('rejects unsigned legacy Twilio callbacks and never treats parent completion as transfer success', async () => {
    const authToken = 'twilio-test-token';
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare('INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted) VALUES(1,?,?)')
        .bind(encryptFernet(env.ENCRYPTION_KEY, 'AC00000000000000000000000000000000'), encryptFernet(env.ENCRYPTION_KEY, authToken)),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,status,voice_provider,total_numbers) VALUES(1,1,'callback',1,1,'paused','twilio',1)"),
      env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,call_sid,dispatch_state) VALUES(1,1,'+15550000002','calling','CA123','dispatched')"),
    ]);
    const url = 'https://example.test/api/provider/callback/twilio/1';
    const body = new URLSearchParams({ CallSid: 'CA123', CallStatus: 'completed', CallDuration: '12', SequenceNumber: '4' });
    const unsigned = await exports.default.fetch(new Request(url, {
      method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body,
    }));
    expect(unsigned.status).toBe(403);

    const signature = await twilioSignature(url, body, authToken);
    const signedRequest = () => new Request(url, {
      method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded', 'X-Twilio-Signature': signature }, body: body.toString(),
    });
    const accepted = await exports.default.fetch(signedRequest());
    expect(accepted.status).toBe(200);
    const row = await env.DB.prepare('SELECT status,provider_status,duration_seconds,dispatch_state FROM campaign_numbers WHERE id=1').first<Record<string, any>>();
    expect(row).toMatchObject({ status: 'failed', provider_status: 'completed', duration_seconds: 12, dispatch_state: 'finished' });

    const duplicate = await exports.default.fetch(signedRequest());
    expect(await duplicate.json()).toMatchObject({ ok: true, duplicate: true });
    const events = await env.DB.prepare('SELECT COUNT(*) count FROM provider_events').first<{ count: number }>();
    expect(events?.count).toBe(1);
  });

  it('treats Twilio parent completion as success for ai_agent campaigns', async () => {
    const authToken = 'twilio-test-token';
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare('INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted) VALUES(1,?,?)')
        .bind(encryptFernet(env.ENCRYPTION_KEY, 'AC00000000000000000000000000000000'), encryptFernet(env.ENCRYPTION_KEY, authToken)),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,status,voice_provider,campaign_mode,total_numbers) VALUES(1,1,'ai',1,1,'running','twilio','ai_agent',1)"),
      env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,call_sid,dispatch_state) VALUES(1,1,'+15550000002','calling','CA123','dispatched')"),
    ]);
    const url = 'https://example.test/api/provider/callback/twilio/1';
    const body = new URLSearchParams({ CallSid: 'CA123', CallStatus: 'completed', CallDuration: '30', SequenceNumber: '4' });
    const signature = await twilioSignature(url, body, authToken);
    const response = await exports.default.fetch(new Request(url, {
      method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded', 'X-Twilio-Signature': signature }, body: body.toString(),
    }));
    expect(response.status).toBe(200);
    const row = await env.DB.prepare('SELECT status,outcome_reason FROM campaign_numbers WHERE id=1').first<Record<string, any>>();
    expect(row).toMatchObject({ status: 'completed', outcome_reason: null });
  });

  it('accepts a number-scoped SignalWire status token and returns empty LaML', async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,status,voice_provider,total_numbers) VALUES(1,1,'callback',1,1,'paused','signalwire',1)"),
      env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,call_sid,dispatch_state) VALUES(11,1,'+15550000002','calling','SW123','dispatched')"),
    ]);
    const token = await createProviderCallbackToken(env, 'signalwire-status', 11);
    const response = await exports.default.fetch(new Request(
      `https://example.test/api/provider/callback/signalwire/11?token=${encodeURIComponent(token)}`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ CallSid: 'SW123', CallStatus: 'completed', CallDuration: '7', SequenceNumber: '4' }),
      },
    ));
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toContain('application/xml');
    expect(await response.text()).toBe('<Response/>');
    expect(await env.DB.prepare('SELECT status FROM campaign_numbers WHERE id=11').first()).toMatchObject({ status: 'completed' });
  });

  it('promotes an awaiting SignalWire dispatch when an authenticated callback supplies the SID', async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,status,voice_provider,total_numbers,processed_numbers,failed_calls,completed_at) VALUES(1,1,'callback',1,1,'completed','signalwire',1,1,1,?)")
        .bind(new Date().toISOString()),
      env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,dispatch_state,error_message,processed_at,next_action_at) VALUES(11,1,'+15550000002','dispatch_unknown','dispatch_unknown','empty response',?,?)")
        .bind(new Date().toISOString(), new Date(Date.now() - 1_000).toISOString()),
    ]);
    const token = await createProviderCallbackToken(env, 'signalwire-status', 11);
    const response = await exports.default.fetch(new Request(
      `https://example.test/api/provider/callback/signalwire/11?token=${encodeURIComponent(token)}`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ CallSid: 'SW-LATE', CallStatus: 'initiated', SequenceNumber: '1' }),
      },
    ));
    expect(response.status).toBe(200);
    expect(await env.DB.prepare(
      'SELECT status,dispatch_state,call_sid,provider_status,error_message,processed_at FROM campaign_numbers WHERE id=11',
    ).first()).toMatchObject({
      status: 'calling', dispatch_state: 'dispatched', call_sid: 'SW-LATE', provider_status: 'initiated',
      error_message: null, processed_at: null,
    });
    expect(await env.DB.prepare(
      'SELECT status,processed_numbers,successful_calls,failed_calls,completed_at FROM campaigns WHERE id=1',
    ).first()).toMatchObject({
      status: 'running', processed_numbers: 0, successful_calls: 0, failed_calls: 0, completed_at: null,
    });
  });

  it('accepts a terminal late SignalWire callback without redialing and refreshes campaign totals', async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,status,voice_provider,total_numbers,processed_numbers,failed_calls,completed_at) VALUES(1,1,'callback',1,1,'completed','signalwire',1,1,1,?)")
        .bind(new Date().toISOString()),
      env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,dispatch_state,error_message,processed_at) VALUES(11,1,'+15550000002','dispatch_unknown','dispatch_unknown','empty response',?)")
        .bind(new Date().toISOString()),
    ]);
    const token = await createProviderCallbackToken(env, 'signalwire-status', 11);
    const response = await exports.default.fetch(new Request(
      `https://example.test/api/provider/callback/signalwire/11?token=${encodeURIComponent(token)}`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ CallSid: 'SW-LATE', CallStatus: 'completed', CallDuration: '9', SequenceNumber: '4' }),
      },
    ));
    expect(response.status).toBe(200);
    expect(await env.DB.prepare(
      'SELECT status,dispatch_state,call_sid,provider_status,duration_seconds,error_message FROM campaign_numbers WHERE id=11',
    ).first()).toMatchObject({
      status: 'completed', dispatch_state: 'finished', call_sid: 'SW-LATE', provider_status: 'completed',
      duration_seconds: 9, error_message: null,
    });
    expect(await env.DB.prepare(
      'SELECT status,processed_numbers,successful_calls,failed_calls FROM campaigns WHERE id=1',
    ).first()).toMatchObject({ status: 'completed', processed_numbers: 1, successful_calls: 1, failed_calls: 0 });
  });
});
