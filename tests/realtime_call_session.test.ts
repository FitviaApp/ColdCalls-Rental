import { afterEach, describe, expect, it, vi } from 'vitest';
import { env } from 'cloudflare:workers';
import { runInDurableObject } from 'cloudflare:test';
import { encryptFernet } from '../src/auth';
import { createRealtimeToken } from '../src/services/realtime_token';

afterEach(() => vi.unstubAllGlobals());

describe('RealtimeCallSession', () => {
  it('bridges Twilio and OpenAI, streams ElevenLabs frames, persists turns, and hands off', async () => {
    const id = 201;
    await seedRealtimeCampaign(id);
    const openAiMessages: Record<string, any>[] = [];
    const twilioUpdates: URLSearchParams[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith('wss://api.openai.com/')) {
        const pair = new WebSocketPair();
        const [client, server] = Object.values(pair);
        server.accept();
        server.addEventListener('message', (event) => {
          const message = JSON.parse(String(event.data));
          openAiMessages.push(message);
          if (message.type === 'input_audio_buffer.append') {
            server.send(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: 'I need help' }));
            server.send(JSON.stringify({ type: 'response.output_text.done', text: 'I can help.' }));
            server.send(JSON.stringify({ type: 'response.function_call_arguments.done', name: 'transfer_to_agent', arguments: '{}' }));
          }
        });
        return new Response(null, { status: 101, webSocket: client });
      }
      if (url.includes('api.elevenlabs.io')) {
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array(100).fill(1));
            controller.enqueue(new Uint8Array(220).fill(2));
            controller.close();
          },
        }), { status: 200, headers: { 'content-type': 'audio/basic' } });
      }
      if (url.includes('api.twilio.com') && init?.method === 'POST') {
        twilioUpdates.push(init.body as URLSearchParams);
        return new Response('{}', { status: 200 });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const { stub, socket } = await connect(id);
    const twilioMessages: Record<string, any>[] = [];
    socket.addEventListener('message', (event) => {
      twilioMessages.push(JSON.parse(String(event.data)));
    });
    socket.send(JSON.stringify({ event: 'start', start: { streamSid: 'MZ201' } }));
    socket.send(JSON.stringify({ event: 'media', media: { payload: 'AQID' } }));
    await vi.waitFor(() => {
      expect(openAiMessages.some((event) => event.type === 'session.update')).toBe(true);
      expect(openAiMessages).toContainEqual({ type: 'input_audio_buffer.append', audio: 'AQID' });
    });

    await vi.waitFor(async () => {
      const row = await env.DB.prepare('SELECT ai_turn_count,ai_last_user_input,ai_last_assistant_text FROM campaign_numbers WHERE id=?')
        .bind(id).first<Record<string, any>>();
      expect(row).toMatchObject({ ai_turn_count: 1, ai_last_user_input: 'I need help', ai_last_assistant_text: 'I can help.' });
      expect(twilioMessages.filter((event) => event.event === 'media')).toHaveLength(2);
    });
    const frames = twilioMessages.filter((event) => event.event === 'media');
    expect(frames.map((event) => Uint8Array.from(atob(event.media.payload), (char) => char.charCodeAt(0)).length)).toEqual([160, 160]);

    await vi.waitFor(async () => {
      expect(twilioUpdates).toHaveLength(1);
      expect(twilioUpdates[0].get('Twiml')).toContain('<Dial>+15550000003</Dial>');
      const row = await env.DB.prepare('SELECT ai_handoff_reason FROM campaign_numbers WHERE id=?').bind(id).first<{ ai_handoff_reason: string }>();
      expect(row?.ai_handoff_reason).toBe('agent_requested_transfer');
    });

    socket.send(JSON.stringify({ event: 'stop' }));
    await vi.waitFor(async () => {
      const health = await stub.health(id) as unknown as Record<string, unknown>;
      expect(health.status).toBe('completed');
    });
  });

  it('rejects an invalid media token and records an alarm timeout', async () => {
    const id = 202;
    await seedRealtimeCampaign(id);
    const stub = env.REALTIME_CALLS.get(env.REALTIME_CALLS.idFromName(String(id)));
    const denied = await stub.fetch(new Request(`https://example.test/?campaign_number_id=${id}&token=bad`, { headers: { Upgrade: 'websocket' } }));
    expect(denied.status).toBe(401);

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (!String(input).startsWith('wss://api.openai.com/')) throw new Error(`Unexpected fetch: ${input}`);
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      server.accept();
      return new Response(null, { status: 101, webSocket: client });
    }));
    const connected = await connect(id);
    await runInDurableObject(connected.stub, (instance) => instance.alarm());
    expect(await connected.stub.health(id)).toMatchObject({ status: 'error', error_category: 'timeout' });
    const row = await env.DB.prepare('SELECT ai_runtime_error FROM campaign_numbers WHERE id=?').bind(id).first<{ ai_runtime_error: string }>();
    expect(row?.ai_runtime_error).toContain('[timeout]');
  });
});

async function connect(id: number) {
  const token = await createRealtimeToken(env, id);
  const stub = env.REALTIME_CALLS.get(env.REALTIME_CALLS.idFromName(String(id)));
  const response = await stub.fetch(new Request(`https://example.test/?campaign_number_id=${id}&token=${encodeURIComponent(token)}`, {
    headers: { Upgrade: 'websocket' },
  }));
  expect(response.status).toBe(101);
  const socket = response.webSocket!;
  socket.accept();
  return { stub, socket };
}

async function seedRealtimeCampaign(id: number): Promise<void> {
  const key = env.ENCRYPTION_KEY;
  await env.DB.batch([
    env.DB.prepare("INSERT INTO users(id,email,password_hash,is_admin,is_active,transfer_number) VALUES(1,'owner@example.com','x',0,1,'+15550000003')"),
    env.DB.prepare("INSERT INTO caller_ids(id,user_id,phone_number,country_code,description,is_active) VALUES(1,1,'+15550000001','US','test',1)"),
    env.DB.prepare("INSERT INTO countries(id,code,name,price_per_minute,is_active) VALUES(1,'US','United States',0.01,1)"),
    env.DB.prepare("INSERT INTO ai_agents(id,user_id,name,system_prompt,voice_id,model,is_active) VALUES(1,1,'agent','Be helpful','voice-1','gpt-realtime-2.1',1)"),
    env.DB.prepare('INSERT INTO user_openai_credentials(user_id,api_key_encrypted) VALUES(1,?)').bind(encryptFernet(key, 'sk-test')),
    env.DB.prepare('INSERT INTO user_elevenlabs_credentials(user_id,api_key_encrypted) VALUES(1,?)').bind(encryptFernet(key, 'eleven-test')),
    env.DB.prepare('INSERT INTO user_twilio_credentials(user_id,account_sid_encrypted,auth_token_encrypted) VALUES(1,?,?)')
      .bind(encryptFernet(key, 'AC00000000000000000000000000000000'), encryptFernet(key, 'twilio-test')),
    env.DB.prepare("INSERT INTO campaigns(id,user_id,name,caller_id_id,country_id,ai_agent_id,campaign_mode,status,voice_provider,total_numbers) VALUES(?,1,'realtime',1,1,1,'ai_agent','running','twilio',1)").bind(id),
    env.DB.prepare("INSERT INTO campaign_numbers(id,campaign_id,phone_number,status,call_sid) VALUES(?,?, '+15550000002','calling','CA201')").bind(id, id),
  ]);
}
