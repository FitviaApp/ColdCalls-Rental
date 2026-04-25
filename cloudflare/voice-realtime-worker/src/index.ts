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
    throw new Error(`origin session config failed: ${response.status}`);
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
    throw new Error(`OpenAI realtime websocket failed: ${response.status}`);
  }
  socket.accept();
  return socket;
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
      turn_detection: {
        type: config.turn_detection || "server_vad",
      },
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
    const expected = this.env.EDGE_SESSION_TOKEN || "";
    const provided = getBearerToken(request) || url.searchParams.get("token") || "";
    if (!expected || !timingSafeEqual(provided, expected)) {
      return jsonResponse({ error: "Unauthorized" }, 401);
    }

    const campaignNumberId = url.pathname.split("/").filter(Boolean).pop();
    if (!campaignNumberId) {
      return jsonResponse({ error: "Missing campaign number id" }, 400);
    }

    const pair = new WebSocketPair();
    const [client, providerSocket] = Object.values(pair);
    providerSocket.accept();

    this.handleStream(providerSocket, campaignNumberId).catch((error: unknown) => {
      console.error(JSON.stringify({ event: "stream_error", error: String(error) }));
      providerSocket.close(1011, "realtime bridge failed");
    });

    return new Response(null, { status: 101, webSocket: client });
  }

  private async handleStream(
    providerSocket: WebSocket,
    campaignNumberId: string,
  ): Promise<void> {
    const config = await fetchRuntimeConfig(this.env, campaignNumberId);
    const openaiSocket = await connectOpenAI(config, this.env);
    let streamSid = "";
    let callSid = "";
    let sessionReady = false;
    let transferStarted = false;

    sendJson(openaiSocket, sessionUpdate(config));

    openaiSocket.addEventListener("message", (event: MessageEvent) => {
      const message = JSON.parse(String(event.data)) as JsonObject;
      const type = String(message.type || "");

      if (type === "session.updated" && !sessionReady) {
        sessionReady = true;
        sendJson(openaiSocket, responseCreate());
        return;
      }

      if (type === "response.audio.delta" && streamSid) {
        const delta = String(message.delta || "");
        if (delta) {
          sendJson(providerSocket, providerMedia(streamSid, delta));
        }
        return;
      }

      if (type === "input_audio_buffer.speech_started" && streamSid) {
        sendJson(providerSocket, providerClear(streamSid));
        sendJson(openaiSocket, { type: "response.cancel" });
        return;
      }

      if (type === "response.done") {
        const response = message.response as JsonObject | undefined;
        const output = (response?.output as JsonObject[] | undefined) || [];
        const functionCall = output.find(
          (item) => item.type === "function_call" && item.name === "transfer_call",
        );
        if (functionCall && !transferStarted) {
          transferStarted = true;
          this.ctx.waitUntil(transferActiveCall(config, callSid));
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

    openaiSocket.addEventListener("close", () => providerSocket.close(1000, "openai closed"));
    openaiSocket.addEventListener("error", () => providerSocket.close(1011, "openai error"));

    providerSocket.addEventListener("message", (event: MessageEvent) => {
      const message = JSON.parse(String(event.data)) as JsonObject;
      const start = parseStart(message);
      if (start) {
        streamSid = start.streamSid;
        callSid = start.callSid;
        console.log(
          JSON.stringify({
            event: "provider_stream_started",
            provider: config.provider,
            campaignNumberId,
            streamSid,
            callSid,
          }),
        );
        return;
      }

      const payload = mediaPayload(message);
      if (payload) {
        sendJson(openaiSocket, {
          type: "input_audio_buffer.append",
          audio: payload,
        });
      }
    });

    providerSocket.addEventListener("close", () => openaiSocket.close(1000, "provider closed"));
    providerSocket.addEventListener("error", () => openaiSocket.close(1011, "provider error"));
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return jsonResponse({ ok: true });
    }

    if (url.pathname.startsWith("/voice/realtime/")) {
      const campaignNumberId = url.pathname.split("/").filter(Boolean).pop();
      if (!campaignNumberId) {
        return jsonResponse({ error: "Missing campaign number id" }, 400);
      }
      const stub = env.VOICE_CALLS.getByName(campaignNumberId);
      return await stub.fetch(request);
    }

    return jsonResponse({ error: "Not found" }, 404);
  },
};
