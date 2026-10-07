import { DurableObject } from 'cloudflare:workers';
import type { Env } from '../middleware';
import { getUserOpenaiCredentials } from '../services/user_openai_service';
import { getUserElevenlabsCredentials } from '../services/user_elevenlabs_service';
import { getUserTwilioCredentials } from '../services/user_twilio_service';
import { TwilioService } from '../services/twilio_service';
import { verifyRealtimeToken } from '../services/realtime_token';

interface RealtimeContext {
  number_id: number;
  user_id: number;
  call_sid: string | null;
  system_prompt: string;
  model: string;
  voice_id: string;
  transfer_number: string | null;
}

export class RealtimeCallSession extends DurableObject<Env> {
  private twilioSocket: WebSocket | null = null;
  private openaiSocket: WebSocket | null = null;
  private streamSid = '';
  private numberId = 0;
  private turnCount = 0;
  private elevenlabsKey = '';
  private finished = false;
  private generation = 0;

  async health(campaignNumberId: number): Promise<Record<string, unknown>> {
    const stored = await this.ctx.storage.get<Record<string, unknown>>('health');
    return { campaign_number_id: campaignNumberId, status: 'idle', turn_count: 0, ...stored };
  }

  async stop(campaignNumberId: number): Promise<void> {
    this.numberId = campaignNumberId;
    await this.finish('stopped');
    this.twilioSocket?.close(1000, 'stopped');
  }

  async alarm(): Promise<void> {
    const campaignNumberId = await this.ctx.storage.get<number>('campaignNumberId');
    if (!campaignNumberId || this.finished) return;
    this.finished = true;
    this.twilioSocket?.close(1000, 'session timeout');
    this.openaiSocket?.close(1000, 'session timeout');
    await this.recordError(campaignNumberId, 'timeout', 'Realtime session exceeded its maximum duration');
  }

  async fetch(request: Request): Promise<Response> {
    if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') {
      return new Response('Expected WebSocket', { status: 426 });
    }
    const url = new URL(request.url);
    const campaignNumberId = Number(url.searchParams.get('campaign_number_id'));
    const token = url.searchParams.get('token') ?? '';
    if (!campaignNumberId || !(await verifyRealtimeToken(this.env, token, campaignNumberId))) {
      return new Response('Unauthorized', { status: 401 });
    }
    const context = await this.loadContext(campaignNumberId);
    if (!context) return new Response('Realtime campaign not found', { status: 404 });

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();
    // Twilio media streams reconnect onto the same DO; retire the previous session's
    // sockets first so their close/error listeners cannot finish() the new one.
    this.generation += 1;
    const generation = this.generation;
    this.twilioSocket?.close(1000, 'replaced');
    this.openaiSocket?.close(1000, 'replaced');
    this.twilioSocket = server;
    this.numberId = campaignNumberId;
    this.turnCount = 0;
    this.finished = false;
    await this.ctx.storage.put('campaignNumberId', campaignNumberId);
    const maxSeconds = Math.max(60, Number(this.env.REALTIME_SESSION_MAX_SECONDS || 7200));
    await this.ctx.storage.setAlarm(Date.now() + maxSeconds * 1000);
    await this.recordHealth(campaignNumberId, 'connecting');

    try {
      await this.connectOpenAi(context, generation);
    } catch (error) {
      this.finished = true;
      await this.ctx.storage.deleteAlarm();
      await this.recordError(campaignNumberId, 'provider_auth', String(error));
      server.close(1011, 'OpenAI connection failed');
      return new Response(null, { status: 101, webSocket: client });
    }

    server.addEventListener('message', (event) => {
      if (generation === this.generation) this.onTwilioMessage(String(event.data));
    });
    server.addEventListener('close', () => {
      if (generation === this.generation) this.ctx.waitUntil(this.finish('completed'));
    });
    server.addEventListener('error', () => {
      if (generation === this.generation) this.ctx.waitUntil(this.finish('failed'));
    });
    await this.recordHealth(campaignNumberId, 'connected');
    return new Response(null, { status: 101, webSocket: client });
  }

  private async loadContext(campaignNumberId: number): Promise<RealtimeContext | null> {
    return this.env.DB.prepare(
      `SELECT cn.id AS number_id, c.user_id, cn.call_sid, a.system_prompt, a.model,
              a.voice_id, u.transfer_number
         FROM campaign_numbers cn
         JOIN campaigns c ON c.id = cn.campaign_id
         JOIN ai_agents a ON a.id = c.ai_agent_id
         JOIN users u ON u.id = c.user_id
        WHERE cn.id = ? AND c.campaign_mode = 'ai_agent' AND c.voice_provider = 'twilio'`,
    ).bind(campaignNumberId).first<RealtimeContext>();
  }

  private async connectOpenAi(context: RealtimeContext, generation: number): Promise<void> {
    const [apiKey, organizationId] = await getUserOpenaiCredentials(this.env, this.env.DB, context.user_id);
    this.elevenlabsKey = await getUserElevenlabsCredentials(this.env, this.env.DB, context.user_id);
    if (!apiKey) throw new Error('OpenAI API key is missing');
    const base = this.env.OPENAI_REALTIME_URL || 'wss://api.openai.com/v1/realtime';
    const model = context.model.startsWith('gpt-realtime') ? context.model : this.env.OPENAI_REALTIME_MODEL;
    const wsUrl = `${base}?model=${encodeURIComponent(model)}`;
    const headers: Record<string, string> = { Authorization: `Bearer ${apiKey}`, Upgrade: 'websocket' };
    if (organizationId) headers['OpenAI-Organization'] = organizationId;
    const response = await fetch(wsUrl, { headers });
    const socket = response.webSocket;
    if (!socket) throw new Error(`OpenAI WebSocket rejected: ${response.status}`);
    socket.accept();
    this.openaiSocket = socket;
    socket.addEventListener('message', (event) => {
      if (generation === this.generation) this.onOpenAiMessage(String(event.data), context);
    });
    socket.addEventListener('close', () => {
      if (generation === this.generation) this.ctx.waitUntil(this.finish('provider_closed'));
    });
    socket.addEventListener('error', () => {
      if (generation === this.generation) this.ctx.waitUntil(this.recordError(this.numberId, 'media_bridge', 'OpenAI WebSocket error'));
    });
    socket.send(JSON.stringify({
      type: 'session.update',
      session: {
        type: 'realtime',
        model,
        output_modalities: ['text'],
        instructions: context.system_prompt,
        audio: {
          input: {
            format: { type: 'audio/pcmu' },
            transcription: { model: 'gpt-4o-mini-transcribe' },
            turn_detection: { type: 'server_vad' },
          },
        },
        tools: context.transfer_number ? [{
          type: 'function', name: 'transfer_to_agent',
          description: 'Transfer this call to the configured human agent.', parameters: { type: 'object', properties: {} },
        }] : [],
      },
    }));
    socket.send(JSON.stringify({ type: 'response.create' }));
  }

  private onTwilioMessage(raw: string): void {
    try {
      const event = JSON.parse(raw) as Record<string, any>;
      if (event.event === 'start') this.streamSid = String(event.start?.streamSid ?? event.streamSid ?? '');
      if (event.event === 'media' && event.media?.payload && this.openaiSocket?.readyState === WebSocket.OPEN) {
        this.openaiSocket.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: event.media.payload }));
      }
      if (event.event === 'stop') this.ctx.waitUntil(this.finish('completed'));
    } catch {
      this.ctx.waitUntil(this.recordError(this.numberId, 'media_bridge', 'Invalid Twilio media event'));
    }
  }

  private onOpenAiMessage(raw: string, context: RealtimeContext): void {
    try {
      const event = JSON.parse(raw) as Record<string, any>;
      if (event.type === 'response.output_text.done') {
        this.ctx.waitUntil(this.handleAssistantText(String(event.text ?? ''), context.voice_id));
      } else if (event.type === 'response.output_audio_transcript.done') {
        this.ctx.waitUntil(this.handleAssistantText(String(event.transcript ?? ''), context.voice_id));
      } else if (event.type === 'conversation.item.input_audio_transcription.completed') {
        this.ctx.waitUntil(this.env.DB.prepare(
          'UPDATE campaign_numbers SET ai_last_user_input = ?, updated_at = ? WHERE id = ?',
        ).bind(String(event.transcript ?? '').slice(0, 4000), new Date().toISOString(), this.numberId).run().then(() => undefined));
      } else if (event.type === 'response.function_call_arguments.done' && event.name === 'transfer_to_agent') {
        this.ctx.waitUntil(this.handoff(context));
      } else if (event.type === 'error') {
        this.ctx.waitUntil(this.recordError(this.numberId, 'provider_error', JSON.stringify(event.error ?? event)));
      }
    } catch {
      this.ctx.waitUntil(this.recordError(this.numberId, 'media_bridge', 'Invalid OpenAI realtime event'));
    }
  }

  private async handleAssistantText(text: string, voiceId: string): Promise<void> {
    if (!text) return;
    this.turnCount += 1;
    await this.env.DB.prepare(
      'UPDATE campaign_numbers SET ai_turn_count = ?, ai_last_assistant_text = ?, updated_at = ? WHERE id = ?',
    ).bind(this.turnCount, text.slice(0, 4000), new Date().toISOString(), this.numberId).run();
    await this.speakWithElevenLabs(text, voiceId);
  }

  private sendTwilioAudio(payload: string): void {
    if (!payload || !this.streamSid || this.twilioSocket?.readyState !== WebSocket.OPEN) return;
    this.twilioSocket.send(JSON.stringify({ event: 'media', streamSid: this.streamSid, media: { payload } }));
  }

  private async speakWithElevenLabs(text: string, voiceId: string): Promise<void> {
    try {
      const base = this.env.ELEVENLABS_API_BASE.replace(/\/+$/, '');
      const response = await fetch(
        `${base}/text-to-speech/${encodeURIComponent(voiceId)}/stream?output_format=${encodeURIComponent(this.env.ELEVENLABS_OUTPUT_FORMAT)}`,
        {
          method: 'POST',
          headers: { 'xi-api-key': this.elevenlabsKey, 'content-type': 'application/json', accept: 'audio/basic' },
          body: JSON.stringify({ text, model_id: this.env.ELEVENLABS_TTS_MODEL_ID }),
        },
      );
      if (!response.ok) throw new Error(`ElevenLabs TTS failed: ${response.status}`);
      if (!response.body) throw new Error('ElevenLabs TTS returned no audio stream');
      const reader = response.body.getReader();
      let pending = new Uint8Array(0);
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const next = new Uint8Array(pending.length + value.length);
        next.set(pending);
        next.set(value, pending.length);
        let offset = 0;
        while (next.length - offset >= 160) {
          this.sendTwilioAudio(base64(next.subarray(offset, offset + 160)));
          offset += 160;
        }
        pending = next.slice(offset);
      }
      if (pending.length) this.sendTwilioAudio(base64(pending));
    } catch (error) {
      await this.recordError(this.numberId, 'provider_error', String(error));
    }
  }

  private async handoff(context: RealtimeContext): Promise<void> {
    if (!context.call_sid || !context.transfer_number) return;
    const [sid, token] = await getUserTwilioCredentials(this.env, this.env.DB, context.user_id);
    await TwilioService(this.env, sid, token).updateCallTwiml(
      context.call_sid,
      `<Response><Dial>${escapeXml(context.transfer_number)}</Dial></Response>`,
    );
    await this.env.DB.prepare(
      'UPDATE campaign_numbers SET ai_handoff_reason = ?, updated_at = ? WHERE id = ?',
    ).bind('agent_requested_transfer', new Date().toISOString(), context.number_id).run();
  }

  private async finish(status: string): Promise<void> {
    if (this.finished) return;
    this.finished = true;
    await this.ctx.storage.deleteAlarm();
    this.openaiSocket?.close(1000, status);
    await this.recordHealth(this.numberId, status);
  }

  private async recordHealth(campaignNumberId: number, status: string): Promise<void> {
    await this.ctx.storage.put('health', { status, turn_count: this.turnCount, updated_at: new Date().toISOString() });
    if (campaignNumberId) {
      await this.env.DB.prepare('UPDATE campaign_numbers SET updated_at = ? WHERE id = ?')
        .bind(new Date().toISOString(), campaignNumberId).run();
    }
  }

  private async recordError(campaignNumberId: number, category: string, detail: string): Promise<void> {
    const message = `[${category}] ${detail}`.slice(0, 2000);
    await this.ctx.storage.put('health', { status: 'error', turn_count: this.turnCount, error_category: category, error_detail: detail.slice(0, 500) });
    if (campaignNumberId) {
      await this.env.DB.prepare('UPDATE campaign_numbers SET ai_runtime_error = ?, updated_at = ? WHERE id = ?')
        .bind(message, new Date().toISOString(), campaignNumberId).run();
    }
  }
}

function escapeXml(value: string): string {
  return value.replace(/[<>&"']/g, (char) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' })[char]!);
}

function base64(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
