import { afterEach, describe, expect, it, vi } from 'vitest';
import { env, exports } from 'cloudflare:workers';
import { getExpectedTwilioSignature } from 'twilio/lib/webhooks/webhooks.js';
import { decryptFernet, encryptFernet, hashPassword, signToken } from '../src/auth';
import { createTwilioAttemptToken, type TwilioCallbackPurpose } from '../src/services/provider_callback_token';
import { ensureTwilioBasicCampaignReady } from '../src/services/twilio_basic_readiness';
import { validateTwilioWebhook } from '../src/services/twilio_webhook';

const ACCOUNT_SID = 'AC00000000000000000000000000000000';
const AUTH_TOKEN = 'twilio-test-token';
const ATTEMPT = 'attempt-basic-1';
const CSRF = '00000000-0000-4000-8000-000000000002';

function params(body: URLSearchParams): Record<string, string | string[]> {
  const output: Record<string, string | string[]> = {};
  for (const key of new Set(body.keys())) {
    const values = body.getAll(key);
    output[key] = values.length === 1 ? values[0] : values;
  }
  return output;
}

function signedRequest(url: string, body: URLSearchParams, token = AUTH_TOKEN): Request {
  return new Request(url, {
    method: 'POST',
    headers: {
      'content-type': 'application/x-www-form-urlencoded',
      'X-Twilio-Signature': getExpectedTwilioSignature(token, url, params(body)),
    },
    body: body.toString(),
  });
}

async function callbackUrl(numberId: number, purpose: TwilioCallbackPurpose): Promise<string> {
  const token = await createTwilioAttemptToken(env, { numberId, attemptId: ATTEMPT, purpose });
  return `https://example.test/api/twilio/calls/${numberId}/${purpose === 'parent-status' ? 'status' : purpose}?token=${encodeURIComponent(token)}`;
}

async function seedBasicCall(numberId = 1, press1 = true, contentType = 'audio/mpeg'): Promise<void> {
  await env.DB.batch([
    env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'twilio@example.com','x',0,1,'+15550000003')"),
    env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','twilio',1)"),
    env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
    env.DB.prepare('INSERT INTO audios(id,user_id,name,r2_key,r2_url,content_type,is_active) VALUES(1,1,\'intro\',\'audios/intro.mp3\',\'\',?,1)').bind(contentType),
    env.DB.prepare("INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted,verification_status,verified_at) VALUES(1,?,?,'verified',?)")
      .bind(encryptFernet(env.ENCRYPTION_KEY, ACCOUNT_SID), encryptFernet(env.ENCRYPTION_KEY, AUTH_TOKEN), new Date().toISOString()),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,audio_id,campaign_mode,status,press_1_to_talk_with_agent,voice_provider,total_numbers) VALUES(1,1,'basic',1,1,1,'audio','running',?,'twilio',1)").bind(press1 ? 1 : 0),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,dispatch_state,dispatch_attempt_id) VALUES(?,1,'+15550000002','calling','dispatched',?)").bind(numberId, ATTEMPT),
  ]);
  await env.AUDIO_R2.put('audios/intro.mp3', new Uint8Array([1, 2, 3]), { httpMetadata: { contentType } });
}

function extractUrl(xml: string, attribute: 'action' | 'statusCallback'): string {
  const match = xml.match(new RegExp(`${attribute}="([^"]+)"`));
  if (!match) throw new Error(`Missing ${attribute}`);
  return match[1].replace(/&amp;/g, '&');
}

afterEach(() => vi.unstubAllGlobals());

describe('Twilio basic call hardening', () => {
  it('uses the official validator for duplicate parameters, encoded queries, ports, and bad signatures', async () => {
    const url = 'https://example.test:8443/webhook?token=a%2Bb&next=%2Fcalls';
    const body = new URLSearchParams();
    body.append('Foo', 'z');
    body.append('Foo', 'a');
    body.append('Foo', 'z');
    body.set('CallSid', 'CA1');
    expect(await validateTwilioWebhook(signedRequest(url, body), AUTH_TOKEN)).not.toBeNull();
    const invalid = signedRequest(url, body);
    invalid.headers.set('X-Twilio-Signature', 'invalid');
    expect(await validateTwilioWebhook(invalid, AUTH_TOKEN)).toBeNull();
  });

  it('hangs up on machine detection without playing or transferring', async () => {
    await seedBasicCall(1, false);
    const url = await callbackUrl(1, 'answer');
    const response = await exports.default.fetch(signedRequest(url, new URLSearchParams({
      CallSid: 'CA-PARENT', AnsweredBy: 'machine_start',
    })));
    expect(await response.text()).toBe('<Response><Hangup/></Response>');
    expect(await env.DB.prepare('SELECT status,answered_by,outcome_reason,transfer_call_sid FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      status: 'failed', answered_by: 'machine_start', outcome_reason: 'machine', transfer_call_sid: null,
    });
  });

  it('preserves an unknown AMD result and its detection duration for diagnosis', async () => {
    await seedBasicCall(1, false);
    const url = await callbackUrl(1, 'answer');
    const response = await exports.default.fetch(signedRequest(url, new URLSearchParams({
      CallSid: 'CA-PARENT', AnsweredBy: 'unknown', MachineDetectionDuration: '5007',
    })));
    expect(await response.text()).toBe('<Response><Hangup/></Response>');
    expect(await env.DB.prepare('SELECT status,answered_by,outcome_reason,error_message FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      status: 'failed',
      answered_by: 'unknown',
      outcome_reason: 'amd_unknown',
      error_message: 'Twilio AMD could not classify the answer after 5007 ms',
    });
  });

  it('records Press 1 timeout as failure and uses actionOnEmptyResult', async () => {
    await seedBasicCall(1, true);
    const answerUrl = await callbackUrl(1, 'answer');
    const answer = await exports.default.fetch(signedRequest(answerUrl, new URLSearchParams({
      CallSid: 'CA-PARENT',
    })));
    const twiml = await answer.text();
    expect(twiml).toContain('actionOnEmptyResult="true"');
    expect(twiml).toContain('<Play>');
    expect(await env.DB.prepare('SELECT answered_by,outcome_reason FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      answered_by: null, outcome_reason: 'awaiting_press_1',
    });
    const gatherUrl = extractUrl(twiml, 'action');
    const gather = await exports.default.fetch(signedRequest(gatherUrl, new URLSearchParams({ CallSid: 'CA-PARENT' })));
    expect(await gather.text()).toBe('<Response><Hangup/></Response>');
    expect(await env.DB.prepare('SELECT status,outcome_reason FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      status: 'failed', outcome_reason: 'no_input',
    });
  });

  it('uses digit 1, not AMD, to verify a human before transfer', async () => {
    await seedBasicCall(1, true);
    const answerUrl = await callbackUrl(1, 'answer');
    const answer = await exports.default.fetch(signedRequest(answerUrl, new URLSearchParams({
      CallSid: 'CA-PARENT',
    })));
    const gatherUrl = extractUrl(await answer.text(), 'action');
    const gather = await exports.default.fetch(signedRequest(gatherUrl, new URLSearchParams({
      CallSid: 'CA-PARENT', Digits: '1',
    })));
    expect(await gather.text()).toContain('<Dial');
    expect(await env.DB.prepare('SELECT answered_by,outcome_reason,pressed_1_at FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      answered_by: 'human', outcome_reason: 'pressed_1',
    });
  });

  it('does not count parent completion as success and accepts an out-of-order connected transfer once', async () => {
    await seedBasicCall(1, false);
    const answerUrl = await callbackUrl(1, 'answer');
    const answer = await exports.default.fetch(signedRequest(answerUrl, new URLSearchParams({
      CallSid: 'CA-PARENT', AnsweredBy: 'human',
    })));
    const transferUrl = extractUrl(await answer.text(), 'statusCallback');

    const parentUrl = await callbackUrl(1, 'parent-status');
    const parent = await exports.default.fetch(signedRequest(parentUrl, new URLSearchParams({
      CallSid: 'CA-PARENT', CallStatus: 'completed', CallDuration: '12', SequenceNumber: '4',
    })));
    expect(parent.status).toBe(200);
    expect(await env.DB.prepare('SELECT status,outcome_reason FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      status: 'calling', outcome_reason: 'awaiting_transfer_status',
    });

    const connectedBody = new URLSearchParams({
      ParentCallSid: 'CA-PARENT', CallSid: 'CA-CHILD', CallStatus: 'in-progress', SequenceNumber: '2',
    });
    expect((await exports.default.fetch(signedRequest(transferUrl, connectedBody))).status).toBe(200);
    expect(await env.DB.prepare('SELECT status,transfer_call_sid,transfer_status,outcome_reason FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      status: 'completed', transfer_call_sid: 'CA-CHILD', transfer_status: 'in-progress', outcome_reason: 'transfer_connected',
    });

    const stale = new URLSearchParams({
      ParentCallSid: 'CA-PARENT', CallSid: 'CA-CHILD', CallStatus: 'ringing', SequenceNumber: '1',
    });
    expect(await (await exports.default.fetch(signedRequest(transferUrl, stale))).json()).toMatchObject({ ok: true, stale: true });
    expect(await env.DB.prepare('SELECT status,transfer_status,transfer_sequence FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      status: 'completed', transfer_status: 'in-progress', transfer_sequence: 2,
    });

    const laterFailure = new URLSearchParams({
      ParentCallSid: 'CA-PARENT', CallSid: 'CA-CHILD', CallStatus: 'failed', SequenceNumber: '3',
    });
    expect((await exports.default.fetch(signedRequest(transferUrl, laterFailure))).status).toBe(200);
    expect(await env.DB.prepare('SELECT status,transfer_status,outcome_reason FROM campaign_numbers WHERE id=1').first()).toMatchObject({
      status: 'completed', transfer_status: 'failed', outcome_reason: 'transfer_connected',
    });
  });

  it('rejects a callback from a mismatched parent CallSid', async () => {
    await seedBasicCall(1, false);
    await env.DB.prepare("UPDATE campaign_numbers SET call_sid='CA-RIGHT' WHERE id=1").run();
    const transferToken = await createTwilioAttemptToken(env, { numberId: 1, attemptId: ATTEMPT, purpose: 'transfer-status' });
    const url = `https://example.test/api/twilio/calls/1/transfer-status?token=${encodeURIComponent(transferToken)}`;
    const response = await exports.default.fetch(signedRequest(url, new URLSearchParams({
      ParentCallSid: 'CA-WRONG', CallSid: 'CA-CHILD', CallStatus: 'in-progress', SequenceNumber: '1',
    })));
    expect(response.status).toBe(409);
  });

  it('preserves verified credentials when replacement validation fails, then saves with readback', async () => {
    const passwordHash = hashPassword('password');
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active) VALUES(1,'owner@example.com',?,1,1)").bind(passwordHash),
      env.DB.prepare("INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted,verification_status,verified_at) VALUES(1,?,?,'verified',?)")
        .bind(encryptFernet(env.ENCRYPTION_KEY, ACCOUNT_SID), encryptFernet(env.ENCRYPTION_KEY, 'old-token'), new Date().toISOString()),
    ]);
    const login = await signToken(env.JWT_SECRET, 1);
    const request = (sid: string, token: string) => new Request('https://example.test/dashboard/settings/twilio', {
      method: 'POST',
      redirect: 'manual',
      headers: {
        accept: 'text/html',
        cookie: `access_token=${login}; csrf_token=${CSRF}`,
        'content-type': 'application/x-www-form-urlencoded',
      },
      body: new URLSearchParams({ account_sid: sid, auth_token: token, csrf_token: CSRF }),
    });

    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ message: 'Authenticate' }), { status: 401 })));
    expect((await exports.default.fetch(request(ACCOUNT_SID, 'bad-token'))).status).toBe(400);
    let row = await env.DB.prepare('SELECT account_sid_encrypted,auth_token_encrypted,verification_status FROM user_twilio_credentials WHERE user_id=1').first<Record<string, string>>();
    expect(decryptFernet(env.ENCRYPTION_KEY, row!.auth_token_encrypted)).toBe('old-token');
    expect(row!.verification_status).toBe('verified');

    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response(JSON.stringify({ sid: ACCOUNT_SID, status: 'active' }), { status: 200 })));
    expect((await exports.default.fetch(request(ACCOUNT_SID, 'new-token'))).status).toBe(302);
    row = await env.DB.prepare('SELECT account_sid_encrypted,auth_token_encrypted,verification_status,verified_at FROM user_twilio_credentials WHERE user_id=1').first<Record<string, string>>();
    expect(decryptFernet(env.ENCRYPTION_KEY, row!.auth_token_encrypted)).toBe('new-token');
    expect(row).toMatchObject({ verification_status: 'verified' });
    expect(row!.verified_at).toBeTruthy();
  });

  it('blocks OGG for Twilio but accepts WAV after live caller-ID validation', async () => {
    await seedBasicCall(1, false, 'audio/ogg');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      incoming_phone_numbers: [{ sid: 'PN1', phone_number: '+15550000001' }],
    }), { status: 200 })));
    await expect(ensureTwilioBasicCampaignReady(env, 1, 1)).rejects.toThrow('not supported');
    await env.DB.prepare("UPDATE audios SET content_type='audio/wav' WHERE id=1").run();
    await expect(ensureTwilioBasicCampaignReady(env, 1, 1)).resolves.toBeUndefined();
  });
});
