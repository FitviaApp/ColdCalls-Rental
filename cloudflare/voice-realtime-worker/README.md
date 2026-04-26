# ColdCalls Voice Realtime Worker

Cloudflare Worker POC for bridging Twilio/SignalWire bidirectional media
streams to OpenAI Realtime.

## Required configuration

Set non-secret vars in `wrangler.jsonc`:

- `ORIGIN_BASE_URL`: public FastAPI origin, for example `https://app.example.com`
- `OPENAI_REALTIME_BASE_URL`: defaults to `https://api.openai.com/v1/realtime`

Set the shared secret as a Worker secret:

```bash
npx wrangler secret put EDGE_SESSION_TOKEN
```

The same value must be configured in FastAPI as `AI_REALTIME_EDGE_SECRET`.
The generated media stream URL includes this secret as a `token` query
parameter because not every telephony provider forwards custom authorization
attributes during the WebSocket upgrade.

## FastAPI environment

```env
AI_REALTIME_ENABLED=true
AI_REALTIME_STREAM_BASE_URL=https://coldcalls-voice-realtime.<account>.workers.dev
AI_REALTIME_EDGE_SECRET=<same secret>
AI_REALTIME_MODEL=gpt-realtime
AI_REALTIME_VOICE=verse
```

When enabled, AI-agent campaigns return `<Connect><Stream>` instead of the
turn-based `<Gather>` flow.
