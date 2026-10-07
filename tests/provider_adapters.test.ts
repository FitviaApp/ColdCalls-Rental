import { afterEach, describe, expect, it, vi } from 'vitest';
import { env } from 'cloudflare:workers';
import { TwilioService } from '../src/services/twilio_service';
import { SignalWireService } from '../src/services/signalwire_service';
import { TelnyxService } from '../src/services/telnyx_service';
import { VonageService } from '../src/services/vonage_service';
import { VoximplantService } from '../src/services/voximplant_service';
import { isPublicCallbackUrl } from '../src/services/callback_url_service';

const call = {
  toNumber: '+15550000002',
  fromNumber: '+15550000001',
  audioUrl: 'https://example.test/api/media/audio/1?token=signed',
  transferNumber: '+15550000003',
  campaignId: 7,
  metadata: { campaign_number_id: 11 },
  dispatchAttemptId: 'attempt-11',
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('voice provider contracts', () => {
  it('accepts multi-label public hosts without confusing them with IPv4 addresses', () => {
    expect(isPublicCallbackUrl('https://coldcalls.mralnilam.workers.dev/api/callback')).toBe(true);
    expect(isPublicCallbackUrl('https://127.0.0.1/api/callback')).toBe(false);
    expect(isPublicCallbackUrl('http://192.168.1.10/api/callback')).toBe(false);
  });

  it('creates and reads a Twilio call with authenticated callbacks', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ sid: 'CA11', status: 'queued' }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'completed', duration: '9', answered_by: 'human' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const service = TwilioService(env, 'AC00000000000000000000000000000000', 'token');
    expect(await service.makeCall(call)).toEqual({ call_sid: 'CA11', status: 'queued' });
    const request = fetchMock.mock.calls[0];
    expect(String(request[0])).toContain('/Calls.json');
    const body = request[1]?.body as URLSearchParams;
    expect(new URL(body.get('StatusCallback')!).pathname).toBe('/api/twilio/calls/11/status');
    expect(new URL(body.get('Url')!).pathname).toBe('/api/twilio/calls/11/answer');
    expect(body.get('MachineDetection')).toBe('Enable');
    expect(body.get('Twiml')).toBeNull();
    expect(body.getAll('StatusCallbackEvent')).toEqual(['initiated', 'ringing', 'answered', 'completed']);
    expect(await service.getCallStatus('CA11')).toMatchObject({ status: 'completed', duration: 9, answered_by: 'human' });
  });

  it('recognizes an owned Twilio caller ID', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({
      incoming_phone_numbers: [{ sid: 'PN11', phone_number: '+15550000001' }],
    }), { status: 200, headers: { 'content-type': 'application/json' } })));
    const service = TwilioService(env, 'AC00000000000000000000000000000000', 'token');
    expect(await service.verifyCallerId('+15550000001')).toEqual({
      valid: true,
      status: 'owned',
      providerSid: 'PN11',
    });
  });

  it('creates and reads a SignalWire call with authenticated callbacks', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ sid: 'SW11', status: 'queued' }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'completed', duration: '8' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const service = SignalWireService(env, 'project', 'token', 'space.signalwire.com');
    expect(await service.makeCall(call)).toEqual({ call_sid: 'SW11', status: 'queued' });
    const body = fetchMock.mock.calls[0][1]?.body as URLSearchParams;
    const statusCallback = new URL(body.get('StatusCallback')!);
    expect(statusCallback.origin + statusCallback.pathname).toBe('https://example.test/api/provider/callback/signalwire/11');
    expect(statusCallback.searchParams.get('token')).toBeTruthy();
    expect(await service.getCallStatus('SW11')).toMatchObject({ status: 'completed', duration: 8 });
  });

  it('treats a successful empty SignalWire response as awaiting a callback', async () => {
    const log = vi.spyOn(console, 'info').mockImplementation(() => undefined);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(null, {
      status: 204,
      headers: { 'x-request-id': 'req-empty' },
    })));
    const service = SignalWireService(env, 'project', 'token', 'space.signalwire.com');
    await expect(service.makeCall(call)).resolves.toEqual({
      call_sid: null,
      status: 'accepted',
      accepted_without_sid: true,
      response_diagnostic: 'SignalWire create call accepted without call SID: status=204 reason=empty_body request_id=req-empty',
    });
    expect(JSON.parse(String(log.mock.calls[0]?.[0]))).toMatchObject({
      event: 'signalwire_create_call_response', status: 204, request_id: 'req-empty',
    });
  });

  it.each([
    ['invalid_json', '<html>accepted</html>', 'text/html'],
    ['missing_sid', JSON.stringify({ status: 'queued' }), 'application/json'],
  ])('waits for a callback when SignalWire returns %s', async (reason, body, contentType) => {
    vi.spyOn(console, 'info').mockImplementation(() => undefined);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(body, {
      status: 201,
      headers: { 'content-type': contentType },
    })));
    const service = SignalWireService(env, 'project', 'token', 'space.signalwire.com');
    const result = await service.makeCall(call);
    expect(result).toMatchObject({ call_sid: null, accepted_without_sid: true });
    expect(result.response_diagnostic).toContain(`reason=${reason}`);
  });

  it('keeps SignalWire 4xx failures explicit and network failures ambiguous', async () => {
    vi.spyOn(console, 'info').mockImplementation(() => undefined);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ message: 'invalid destination' }), {
        status: 400,
        headers: { 'content-type': 'application/json' },
      }))
      .mockRejectedValueOnce(new Error('connection reset after write'));
    vi.stubGlobal('fetch', fetchMock);
    const service = SignalWireService(env, 'project', 'token', 'space.signalwire.com');
    await expect(service.makeCall(call)).rejects.toThrow('SignalWire create call failed: status=400');
    await expect(service.makeCall(call)).rejects.toThrow('SignalWire create call failed: Error: connection reset after write');
  });

  it('preserves SignalWire failure diagnostics returned by the call resource', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        status: 'failed', duration: '0', error_code: '11200',
        error_message: 'HTTP retrieval failure', hangup_cause: 'NORMAL_TEMPORARY_FAILURE',
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{
        level: 'error', name: 'calling_laml_webhook_failed',
        details: { response_code: 403, message: 'Remote document returned an error', url: 'https://example.test?token=secret' },
      }, {
        level: 'info', name: 'calling_call_failed',
        details: { reason: 'Exceeded Outbound Call Rate' },
      }] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const service = SignalWireService(env, 'project', 'token', 'space.signalwire.com');
    expect(await service.getCallStatus('SW-failed')).toMatchObject({
      status: 'failed', duration: 0, error_code: 11200,
      error_message: 'HTTP retrieval failure; hangup_cause=NORMAL_TEMPORARY_FAILURE; calling_call_failed reason=Exceeded Outbound Call Rate; calling_laml_webhook_failed response_code=403 message=Remote document returned an error',
    });
    expect(String(fetchMock.mock.calls[1][0])).toContain('/api/voice/logs/SW-failed/events');
  });

  it('gives SignalWire a signed public LaML URL for press-1 audio campaigns', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify({ sid: 'SW12', status: 'queued' }), { status: 201 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const service = SignalWireService(env, 'project', 'token', 'space.signalwire.com');
    await service.makeCall({ ...call, press1ToTalkWithAgent: true });
    const body = fetchMock.mock.calls[0][1]?.body as URLSearchParams;
    const answerUrl = new URL(body.get('Url')!);
    expect(answerUrl.origin + answerUrl.pathname).toBe('https://example.test/api/twiml/7');
    expect(answerUrl.searchParams.get('token')).toBeTruthy();
    expect(body.get('Twiml')).toBeNull();
  });

  it('creates and reads a Telnyx TeXML call with a scoped callback token', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { sid: 'TX11', status: 'queued' } }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { status: 'completed', duration: 7 } }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const service = TelnyxService(env, 'KEY', 'ACCOUNT');
    expect(await service.makeCall(call)).toEqual({ call_sid: 'TX11', status: 'queued' });
    const body = fetchMock.mock.calls[0][1]?.body as URLSearchParams;
    const callback = new URL(body.get('Url')!);
    expect(callback.pathname).toBe('/api/telnyx/texml/7');
    expect(callback.searchParams.get('token')).toBeTruthy();
    expect(await service.getCallStatus('TX11')).toMatchObject({ status: 'completed', duration: 7 });
  });

  it('creates and reads a Vonage call using a short-lived signed JWT', async () => {
    const privateKey = await rsaPrivateKey();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ uuid: 'VX11', status: 'started' }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'completed', duration: 6 }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const service = VonageService('application-id', privateKey);
    expect(await service.makeCall(call)).toEqual({ call_sid: 'VX11', status: 'started' });
    const options = fetchMock.mock.calls[0][1];
    expect(new Headers(options?.headers).get('authorization')).toMatch(/^Bearer ey/);
    expect(JSON.parse(String(options?.body))).toMatchObject({
      to: [{ type: 'phone', number: call.toNumber }],
      from: { type: 'phone', number: call.fromNumber },
    });
    expect(await service.getCallStatus('VX11')).toMatchObject({ status: 'completed', duration: 6 });
  });

  it('starts Voximplant with a signed callback and reads callback-owned status from D1', async () => {
    const privateKey = await rsaPrivateKey();
    await env.DB.batch([
      env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active) VALUES(1,'owner@example.com','x',0,1)"),
      env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
      env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
      env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,status,voice_provider,total_numbers) VALUES(7,1,'vox',1,1,'running','voximplant',1)"),
      env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,duration_seconds) VALUES(11,7,'+15550000002','completed',5)"),
    ]);
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ result: 1, session_id: 'VOX11' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const service = VoximplantService(env, env.DB, {
      account_id: '1', application_id: '2', service_account_email: 'service@example.com', key_id: 'kid',
      private_key: privateKey, vox_app_id: 2, vox_rule_id: 3, vox_scenario_id: 4,
      provision_status: 'ready', provision_error: null,
    });
    expect(await service.makeCall(call)).toEqual({ call_sid: 'VOX11', status: 'queued' });
    const options = fetchMock.mock.calls[0][1];
    const body = options?.body as URLSearchParams;
    const customData = JSON.parse(body.get('script_custom_data')!);
    expect(customData.status_callback_url).toBe('https://example.test/api/voximplant/callback');
    expect(customData.callback_token).toBeTruthy();
    expect(await service.getCallStatus('VOX11', 11)).toMatchObject({ status: 'completed', duration: 5 });
  });
});

async function rsaPrivateKey(): Promise<string> {
  const pair = await crypto.subtle.generateKey(
    { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    true,
    ['sign', 'verify'],
  ) as CryptoKeyPair;
  const der = new Uint8Array(await crypto.subtle.exportKey('pkcs8', pair.privateKey) as ArrayBuffer);
  let binary = '';
  for (const byte of der) binary += String.fromCharCode(byte);
  const encoded = btoa(binary).replace(/(.{64})/g, '$1\n').trim();
  return `-----BEGIN PRIVATE KEY-----\n${encoded}\n-----END PRIVATE KEY-----`;
}
