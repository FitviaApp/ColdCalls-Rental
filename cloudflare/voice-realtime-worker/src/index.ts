import { DurableObject } from "cloudflare:workers";

interface Env {
  VOICE_CALLS: DurableObjectNamespace<VoiceCallSession>;
  ORIGIN_BASE_URL: string;
  EDGE_SESSION_TOKEN: string;
  OPENAI_REALTIME_BASE_URL: string;
}

interface RuntimeConfig {
  campaign_number_id: number;
  provider: "signalwire" | "twilio";
  model: string;
  voice: string;
  turn_detection: string;
  language: string;
  instructions: string;
  transfer_number: string;
  from_number: string;
  to_number: string;
  openai_api_key: string;
  openai_org_id?: string;
  tools: Array<Record<string, unknown>>;
  signalwire?: {
    project_id: string;
    api_token: string;
    space_url: string;
  };
  twilio?: {
    account_sid: string;
    auth_token: string;
  };
}

interface ProviderStart {
  streamSid: string;
  callSid: string;
}

type JsonObject = Record<string, unknown>;

function jsonResponse(payload: JsonObject, status = 200): Response {
  return Response.json(payload, { status });
}

function logRealtimeEvent(campaignNumberId: string, payload: JsonObject): void {
  console.log(JSON.stringify({ campaignNumberId, ...payload }));
}

async function postOriginEvent(
  env: Env,
  campaignNumberId: string,
  payload: JsonObject,
): Promise<void> {
  if (!env.EDGE_SESSION_TOKEN) {
    return;
  }
  try {
    const response = await fetch(
      `${env.ORIGIN_BASE_URL.replace(/\/$/, "")}/api/ai-runtime/realtime/event/${campaignNumberId}`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.EDGE_SESSION_TOKEN}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      },
    );
    if (!response.ok) {
      const body = await response.text();
      console.error(
        JSON.stringify({
          event: "origin_event_post_failed",
          campaignNumberId,
          status: response.status,
          body: body.slice(0, 240),
        }),
      );
    }
  } catch (error: unknown) {
    console.error(
      JSON.stringify({
        event: "origin_event_post_failed",
        campaignNumberId,
        error: String(error),
      }),
    );
  }
}

function getBearerToken(request: Request): string {
  const header = request.headers.get("authorization") || "";
  return header.startsWith("Bearer ") ? header.slice("Bearer ".length).trim() : "";
}

function timingSafeEqual(left: string, right: string): boolean {
  const encoder = new TextEncoder();
  const leftBytes = encoder.encode(left);
  const rightBytes = encoder.encode(right);
  if (leftBytes.length !== rightBytes.length) {
    return false;
  }
  let diff = 0;
  for (let index = 0; index < leftBytes.length; index += 1) {
    diff |= leftBytes[index] ^ rightBytes[index];
  }
  return diff === 0;
}

function normalizeProvider(value: string | null): string {
  return (value || "").trim().toLowerCase();
}

function parseRealtimePath(pathname: string): { pathToken: string; campaignNumberId: string } {
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length >= 4 && parts[0] === "voice" && parts[1] === "realtime") {
    return { pathToken: parts[2], campaignNumberId: parts[3] };
  }
  if (parts.length >= 3 && parts[0] === "voice" && parts[1] === "realtime") {
    return { pathToken: "", campaignNumberId: parts[2] };
  }
  return { pathToken: "", campaignNumberId: "" };
}

function basicAuth(user: string, password: string): string {
  return `Basic ${btoa(`${user}:${password}`)}`;
}

async function fetchRuntimeConfig(
  env: Env,
  campaignNumberId: string,
): Promise<RuntimeConfig> {
  const response = await fetch(
    `${env.ORIGIN_BASE_URL.replace(/\/$/, "")}/api/ai-runtime/realtime/session/${campaignNumberId}`,
    {
      headers: {
        Authorization: `Bearer ${env.EDGE_SESSION_TOKEN}`,
        Accept: "application/json",
        "Cache-Control": "no-store",
      },
    },
  );
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`origin session config failed: ${response.status} ${body.slice(0, 240)}`);
  }
  return (await response.json()) as RuntimeConfig;
}

async function connectOpenAI(config: RuntimeConfig, env: Env): Promise<WebSocket> {
  const url = new URL(env.OPENAI_REALTIME_BASE_URL || "https://api.openai.com/v1/realtime");
  url.searchParams.set("model", config.model || "gpt-realtime");

  const headers = new Headers({
    Authorization: `Bearer ${config.openai_api_key}`,
    "OpenAI-Beta": "realtime=v1",
    Upgrade: "websocket",
  });
  if (config.openai_org_id) {
    headers.set("OpenAI-Organization", config.openai_org_id);
  }

  const response = await fetch(url.toString(), { headers });
  const socket = response.webSocket;
  if (!socket || response.status !== 101) {
    const body = await response.text();
    throw new Error(`OpenAI realtime websocket failed: ${response.status} ${body.slice(0, 240)}`);
  }
  socket.accept();
  return socket;
}

function turnDetection(config: RuntimeConfig): JsonObject {
  const type = (config.turn_detection || "semantic_vad").trim();
  if (type === "semantic_vad") {
    return {
      type: "semantic_vad",
      eagerness: "auto",
      interrupt_response: false,
    };
  }
  return { type };
}

function sessionUpdate(config: RuntimeConfig): JsonObject {
  return {
    type: "session.update",
    session: {
      modalities: ["text", "audio"],
      instructions: config.instructions,
      voice: config.voice || "verse",
      input_audio_format: "g711_ulaw",
      output_audio_format: "g711_ulaw",
      input_audio_transcription: { model: "whisper-1" },
      turn_detection: turnDetection(config),
      tools: config.tools,
      tool_choice: "auto",
      temperature: 0.7,
    },
  };
}

function responseCreate(text?: string): JsonObject {
  return {
    type: "response.create",
    response: {
      modalities: ["audio", "text"],
      instructions:
        text ||
        "Greet the lead in one short sentence and ask if they have a moment to talk.",
    },
  };
}

function parseStart(message: JsonObject): ProviderStart | null {
  if (message.event !== "start") {
    return null;
  }
  const start = message.start as JsonObject | undefined;
  if (!start) {
    return null;
  }
  return {
    streamSid: String(start.streamSid || message.streamSid || ""),
    callSid: String(start.callSid || ""),
  };
}

function mediaPayload(message: JsonObject): string | null {
  if (message.event !== "media") {
    return null;
  }
  const media = message.media as JsonObject | undefined;
  const payload = String(media?.payload || "");
  return payload || null;
}

function providerMedia(streamSid: string, payload: string): JsonObject {
  return {
    event: "media",
    streamSid,
    media: { payload },
  };
}

function providerClear(streamSid: string): JsonObject {
  return {
    event: "clear",
    streamSid,
  };
}

function sendJson(socket: WebSocket, payload: JsonObject): void {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

async function transferActiveCall(
  config: RuntimeConfig,
  callSid: string,
): Promise<void> {
  if (!callSid || !config.transfer_number) {
    return;
  }

  const twiml = `<Response><Dial callerId="${config.from_number}" timeout="30"><Number>${config.transfer_number}</Number></Dial></Response>`;
  if (config.provider === "twilio" && config.twilio) {
    await fetch(
      `https://api.twilio.com/2010-04-01/Accounts/${config.twilio.account_sid}/Calls/${callSid}.json`,
      {
        method: "POST",
        headers: {
          Authorization: basicAuth(config.twilio.account_sid, config.twilio.auth_token),
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: new URLSearchParams({ Twiml: twiml }),
      },
    );
    return;
  }

  if (config.provider === "signalwire" && config.signalwire) {
    await fetch(
      `https://${config.signalwire.space_url}/api/laml/2010-04-01/Accounts/${config.signalwire.project_id}/Calls/${callSid}.json`,
      {
        method: "POST",
        headers: {
          Authorization: basicAuth(config.signalwire.project_id, config.signalwire.api_token),
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: new URLSearchParams({ Twiml: twiml }),
      },
    );
  }
}

export class VoiceCallSession extends DurableObject<Env> {
  async fetch(request: Request): Promise<Response> {
    if (normalizeProvider(request.headers.get("upgrade")) !== "websocket") {
      return jsonResponse({ error: "Expected websocket upgrade" }, 426);
    }

    const url = new URL(request.url);
    const { pathToken, campaignNumberId } = parseRealtimePath(url.pathname);
    if (!campaignNumberId) {
      return jsonResponse({ error: "Missing campaign number id" }, 400);
    }

    const expected = this.env.EDGE_SESSION_TOKEN || "";
    const headerToken = getBearerToken(request);
    const queryToken = url.searchParams.get("token") || "";
    const provided = pathToken || headerToken || queryToken;
    if (!expected || !timingSafeEqual(provided, expected)) {
      logRealtimeEvent(campaignNumberId, {
        event: "provider_stream_unauthorized",
        severity: "error",
        hasEdgeToken: Boolean(expected),
        hasAuthorizationHeader: Boolean(headerToken),
        hasQueryToken: Boolean(queryToken),
        hasPathToken: Boolean(pathToken),
      });
      return jsonResponse({ error: "Unauthorized" }, 401);
    }

    const pair = new WebSocketPair();
    const [client, providerSocket] = Object.values(pair);
    providerSocket.accept();

    this.handleStream(providerSocket, campaignNumberId).catch((error: unknown) => {
      const payload = { event: "stream_error", severity: "error", error: String(error) };
      logRealtimeEvent(campaignNumberId, payload);
      this.ctx.waitUntil(postOriginEvent(this.env, campaignNumberId, payload));
      providerSocket.close(1011, "realtime bridge failed");
    });

    return new Response(null, { status: 101, webSocket: client });
  }

  private async handleStream(
    providerSocket: WebSocket,
    campaignNumberId: string,
  ): Promise<void> {
    let streamSid = "";
    let callSid = "";
    let config: RuntimeConfig | null = null;
    let openaiSocket: WebSocket | null = null;
    let sessionReady = false;
    let providerClosed = false;
    let transferStarted = false;
    let firstProviderMediaSeen = false;
    let firstOpenAIAudioSeen = false;
    let responseInProgress = false;
    let activeAssistantItemId = "";
    let assistantPlaybackStartedAt = 0;
    const pendingProviderAudio: string[] = [];

    const emitEvent = (payload: JsonObject, sendToOrigin = true): void => {
      logRealtimeEvent(campaignNumberId, payload);
      if (sendToOrigin) {
        this.ctx.waitUntil(postOriginEvent(this.env, campaignNumberId, payload));
      }
    };

    const closeOpenAI = (code: number, reason: string): void => {
      if (openaiSocket && openaiSocket.readyState === WebSocket.OPEN) {
        openaiSocket.close(code, reason);
      }
    };

    const forwardProviderAudio = (payload: string): void => {
      if (openaiSocket && openaiSocket.readyState === WebSocket.OPEN) {
        sendJson(openaiSocket, {
          type: "input_audio_buffer.append",
          audio: payload,
        });
        return;
      }
      pendingProviderAudio.push(payload);
    };

    const flushProviderAudio = (): void => {
      if (!openaiSocket || openaiSocket.readyState !== WebSocket.OPEN) {
        return;
      }
      while (pendingProviderAudio.length > 0) {
        const payload = pendingProviderAudio.shift();
        if (!payload) {
          continue;
        }
        sendJson(openaiSocket, {
          type: "input_audio_buffer.append",
          audio: payload,
        });
      }
    };

    const handleProviderMessage = (message: JsonObject): void => {
      const start = parseStart(message);
      if (start) {
        streamSid = start.streamSid;
        callSid = start.callSid;
        emitEvent({
          event: "provider_stream_started",
          severity: "info",
          provider: config?.provider || "unknown",
          streamSid,
          callSid,
        });
        return;
      }

      const payload = mediaPayload(message);
      if (payload) {
        if (!firstProviderMediaSeen) {
          firstProviderMediaSeen = true;
          emitEvent({
            event: "provider_media_first",
            severity: "info",
            streamSid,
            callSid,
          });
        }
        forwardProviderAudio(payload);
      }
    };

    providerSocket.addEventListener("message", (event: MessageEvent) => {
      try {
        handleProviderMessage(JSON.parse(String(event.data)) as JsonObject);
      } catch (error: unknown) {
        emitEvent({
          event: "provider_message_parse_error",
          severity: "error",
          streamSid,
          callSid,
          error: String(error),
        });
        providerSocket.close(1003, "invalid provider message");
        closeOpenAI(1003, "invalid provider message");
      }
    });

    providerSocket.addEventListener("close", (event: CloseEvent) => {
      providerClosed = true;
      emitEvent({
        event: openaiSocket ? "provider_socket_close" : "provider_closed_before_openai_ready",
        severity: openaiSocket ? "info" : "warning",
        provider: config?.provider || "unknown",
        streamSid,
        callSid,
        code: event.code,
        reason: event.reason || "",
      });
      closeOpenAI(1000, "provider closed");
    });
    providerSocket.addEventListener("error", () => {
      providerClosed = true;
      emitEvent({
        event: "provider_socket_error",
        severity: "error",
        provider: config?.provider || "unknown",
        streamSid,
        callSid,
      });
      closeOpenAI(1011, "provider error");
    });

    try {
      config = await fetchRuntimeConfig(this.env, campaignNumberId);
    } catch (error: unknown) {
      emitEvent({
        event: "origin_config_failed",
        severity: "error",
        error: String(error),
      });
      throw error;
    }
    emitEvent(
      {
        event: "runtime_config_loaded",
        severity: "info",
        provider: config.provider,
        model: config.model,
        voice: config.voice,
      },
      false,
    );
    try {
      openaiSocket = await connectOpenAI(config, this.env);
    } catch (error: unknown) {
      emitEvent({
        event: "openai_connect_failed",
        severity: "error",
        provider: config.provider,
        model: config.model,
        error: String(error),
      });
      throw error;
    }
    emitEvent({
        event: "openai_realtime_connected",
        severity: "info",
        provider: config.provider,
        model: config.model,
      });

    if (providerClosed) {
      openaiSocket.close(1000, "provider closed");
      return;
    }

    const activeConfig = config;
    sendJson(openaiSocket, sessionUpdate(activeConfig));
    flushProviderAudio();

    openaiSocket.addEventListener("message", (event: MessageEvent) => {
      let message: JsonObject;
      try {
        message = JSON.parse(String(event.data)) as JsonObject;
      } catch (error: unknown) {
        emitEvent({
          event: "openai_message_parse_error",
          severity: "error",
          streamSid,
          callSid,
          error: String(error),
        });
        providerSocket.close(1003, "invalid openai message");
        closeOpenAI(1003, "invalid openai message");
        return;
      }
      const type = String(message.type || "");

      if (type === "error") {
        emitEvent({
          event: "openai_realtime_error",
          severity: "error",
          streamSid,
          callSid,
          error: JSON.stringify(message.error || {}),
        });
        return;
      }

      if (type === "session.updated" && !sessionReady) {
        sessionReady = true;
        emitEvent({ event: "openai_session_ready", severity: "info", streamSid, callSid });
        sendJson(openaiSocket, responseCreate());
        return;
      }

      if (type === "response.audio.delta" && streamSid) {
        const delta = String(message.delta || "");
        const itemId = String(message.item_id || "");
        if (delta) {
          if (itemId && itemId !== activeAssistantItemId) {
            activeAssistantItemId = itemId;
            assistantPlaybackStartedAt = Date.now();
          }
          if (!firstOpenAIAudioSeen) {
            firstOpenAIAudioSeen = true;
            emitEvent({
              event: "openai_audio_first",
              severity: "info",
              streamSid,
              callSid,
            });
          }
          sendJson(providerSocket, providerMedia(streamSid, delta));
        }
        return;
      }

      if (type === "input_audio_buffer.speech_started" && streamSid) {
        sendJson(providerSocket, providerClear(streamSid));
        if (activeAssistantItemId && assistantPlaybackStartedAt) {
          const audioEndMs = Math.max(0, Date.now() - assistantPlaybackStartedAt);
          sendJson(openaiSocket, {
            type: "conversation.item.truncate",
            item_id: activeAssistantItemId,
            content_index: 0,
            audio_end_ms: audioEndMs,
          });
          activeAssistantItemId = "";
          assistantPlaybackStartedAt = 0;
        }
        if (responseInProgress) {
          sendJson(openaiSocket, { type: "response.cancel" });
        }
        return;
      }

      if (type === "response.created") {
        responseInProgress = true;
        activeAssistantItemId = "";
        assistantPlaybackStartedAt = 0;
        return;
      }

      if (type === "conversation.item.input_audio_transcription.completed") {
        const transcript = String(message.transcript || "").trim();
        if (transcript) {
          emitEvent({
            event: "transcript",
            severity: "info",
            role: "user",
            text: transcript,
            streamSid,
            callSid,
          });
        }
        return;
      }

      if (type === "response.audio_transcript.done") {
        const transcript = String(message.transcript || "").trim();
        if (transcript) {
          emitEvent({
            event: "transcript",
            severity: "info",
            role: "assistant",
            text: transcript,
            streamSid,
            callSid,
          });
        }
        return;
      }

      if (type === "response.done") {
        responseInProgress = false;
        activeAssistantItemId = "";
        assistantPlaybackStartedAt = 0;
        const response = message.response as JsonObject | undefined;
        const output = (response?.output as JsonObject[] | undefined) || [];
        const functionCall = output.find(
          (item) => item.type === "function_call" && item.name === "transfer_call",
        );
        if (functionCall && !transferStarted) {
          transferStarted = true;
          this.ctx.waitUntil(
            transferActiveCall(activeConfig, callSid).catch((error: unknown) => {
              emitEvent({
                event: "transfer_call_failed",
                severity: "error",
                callSid,
                error: String(error),
              });
            }),
          );
          sendJson(openaiSocket, {
            type: "conversation.item.create",
            item: {
              type: "function_call_output",
              call_id: functionCall.call_id,
              output: JSON.stringify({ ok: true }),
            },
          });
          sendJson(
            openaiSocket,
            responseCreate("Tell the lead you are connecting them now, then stop speaking."),
          );
        }
      }
    });

    openaiSocket.addEventListener("close", (event: CloseEvent) => {
      emitEvent({
        event: "openai_socket_close",
        severity: event.code === 1000 ? "info" : "warning",
        streamSid,
        callSid,
        code: event.code,
        reason: event.reason || "",
      });
      providerSocket.close(1000, "openai closed");
    });
    openaiSocket.addEventListener("error", () => {
      emitEvent({
        event: "openai_socket_error",
        severity: "error",
        streamSid,
        callSid,
      });
      providerSocket.close(1011, "openai error");
    });
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return jsonResponse({ ok: true });
    }

    if (url.pathname.startsWith("/voice/realtime/")) {
      const { pathToken, campaignNumberId } = parseRealtimePath(url.pathname);
      if (!campaignNumberId) {
        return jsonResponse({ error: "Missing campaign number id" }, 400);
      }
      logRealtimeEvent(campaignNumberId, {
        event: "worker_realtime_route",
        severity: "info",
        upgrade: request.headers.get("upgrade") || "",
        hasAuthorizationHeader: request.headers.has("authorization"),
        hasQueryToken: url.searchParams.has("token"),
        hasPathToken: Boolean(pathToken),
        hasEdgeToken: Boolean(env.EDGE_SESSION_TOKEN),
      });
      const stub = env.VOICE_CALLS.getByName(campaignNumberId);
      return await stub.fetch(request);
    }

    return jsonResponse({ error: "Not found" }, 404);
  },
};
