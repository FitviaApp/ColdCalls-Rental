"""
Worker-side bridge between SignalWire media events and OpenAI Realtime API.
"""
from __future__ import annotations

import json
import logging
import threading
import asyncio
import time
from typing import Any
from urllib.parse import urlencode
from queue import Queue, Empty

import websockets
try:
    import audioop
except Exception:  # pragma: no cover - stdlib availability differs by Python build
    audioop = None

from app.config import get_settings
from app.models import AIAgent
from app.services.ai_realtime_event_bus import AIRealtimeEvent, AIRealtimeEventBus
from app.services.ai_realtime_session_service import AIRealtimeSessionService

logger = logging.getLogger(__name__)
settings = get_settings()


def classify_openai_error(raw_error: str) -> tuple[str, str]:
    value = str(raw_error or "").strip()
    lowered = value.lower()
    if "401" in lowered or "invalid api key" in lowered or "authentication" in lowered:
        return "provider_auth", value[:500]
    if "429" in lowered or "rate limit" in lowered:
        return "provider_rate_limit", value[:500]
    if "tool" in lowered:
        return "tooling", value[:500]
    if "policy" in lowered:
        return "policy", value[:500]
    return "media_bridge", value[:500]


class AIRealtimeBridgeWorker:
    def __init__(
        self,
        *,
        campaign_number_id: int,
        agent: AIAgent,
        openai_api_key: str,
        openai_org_id: str = "",
    ):
        self.campaign_number_id = int(campaign_number_id)
        self.agent = agent
        self.openai_api_key = (openai_api_key or "").strip()
        self.openai_org_id = (openai_org_id or "").strip()
        self.bus = AIRealtimeEventBus()
        self.sessions = AIRealtimeSessionService()
        self._stop_event = threading.Event()
        self._inbound_audio_queue: Queue[str] = Queue()
        self._opening_response_sent = False
        self._awaiting_audio = False
        self._last_response_request_at = 0.0
        self._last_audio_out_at = 0.0
        self._response_retry_count = 0
        self._voice_fallback_attempted = False
        self._output_audio_codec = "g711_ulaw"
        self._output_pcm_rate_hz = 24000
        self._pcm_ratecv_state = None
        self._pcm16_leftover = b""

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self.openai_api_key:
            self._publish_error("provider_auth", "OpenAI API key is missing")
            return
        try:
            import asyncio

            asyncio.run(self._run_async())
        except Exception as exc:
            category, detail = classify_openai_error(str(exc))
            self._publish_error(category, detail)

    async def _run_async(self) -> None:
        realtime_model = (self.agent.model or settings.OPENAI_REALTIME_MODEL).strip()
        query = urlencode({"model": realtime_model})
        ws_url = f"{settings.OPENAI_REALTIME_URL}?{query}"
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        if self.openai_org_id:
            headers["OpenAI-Organization"] = self.openai_org_id

        async with websockets.connect(ws_url, additional_headers=headers, max_size=4_000_000) as openai_ws:
            await self._send_realtime_session_update(openai_ws)
            self.bus.publish(self.campaign_number_id, "session.started", {"source": "worker"})

            consumer_thread = threading.Thread(target=self._consume_bus_events, daemon=True)
            consumer_thread.start()

            while not self._stop_event.is_set():
                await self._flush_inbound_audio(openai_ws)
                self._maybe_retry_silent_response(openai_ws)
                try:
                    message = await asyncio.wait_for(openai_ws.recv(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if message:
                    await self._handle_openai_message(openai_ws, message)

    async def _send_realtime_session_update(self, openai_ws) -> None:
        tool_schema = {
            "type": "function",
            "name": "transfer_call",
            "description": "Transfer the call to a human agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        }
        event = {
            "type": "session.update",
            "session": {
                "instructions": (
                    f"{self.agent.system_prompt.strip()} "
                    f"Handoff guidance: {(self.agent.handoff_description or 'Transfer only when appropriate.').strip()}"
                ),
                "voice": (self.agent.voice_id or "alloy").strip().lower(),
                "tools": [tool_schema],
                "tool_choice": "auto",
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "turn_detection": {"type": "server_vad"},
                "temperature": float(self.agent.temperature or 0.7),
            },
        }
        await openai_ws.send(json.dumps(event))
        await self._send_opening_response(openai_ws)

    def _set_output_audio_format(self, session_payload: dict[str, Any]) -> None:
        if not isinstance(session_payload, dict):
            return

        codec = str(session_payload.get("output_audio_format") or "").strip().lower()
        pcm_rate = 24000

        audio_cfg = session_payload.get("audio") or {}
        if isinstance(audio_cfg, dict):
            output_cfg = audio_cfg.get("output") or {}
            if isinstance(output_cfg, dict):
                fmt = output_cfg.get("format") or {}
                if isinstance(fmt, dict):
                    fmt_type = str(fmt.get("type") or "").strip().lower()
                    if fmt_type == "audio/pcmu":
                        codec = "g711_ulaw"
                    elif fmt_type == "audio/pcma":
                        codec = "g711_alaw"
                    elif fmt_type == "audio/pcm":
                        codec = "pcm16"
                        try:
                            pcm_rate = int(fmt.get("rate") or 24000)
                        except Exception:
                            pcm_rate = 24000

        if codec not in {"g711_ulaw", "g711_alaw", "pcm16"}:
            codec = "g711_ulaw"

        self._output_audio_codec = codec
        self._output_pcm_rate_hz = max(8000, int(pcm_rate or 24000))
        self._pcm_ratecv_state = None
        self._pcm16_leftover = b""

    def _decode_base64_audio(self, payload: str) -> bytes:
        raw = str(payload or "").strip()
        if not raw:
            return b""
        missing_padding = len(raw) % 4
        if missing_padding:
            raw += "=" * (4 - missing_padding)
        try:
            import base64
            return base64.b64decode(raw)
        except Exception:
            return b""

    def _encode_base64_audio(self, payload: bytes) -> str:
        if not payload:
            return ""
        import base64
        return base64.b64encode(payload).decode("ascii")

    def _normalize_outbound_audio_for_twilio(self, delta_b64: str) -> str:
        audio_bytes = self._decode_base64_audio(delta_b64)
        if not audio_bytes:
            return ""

        codec = self._output_audio_codec
        if codec == "g711_ulaw":
            return self._encode_base64_audio(audio_bytes)

        if audioop is None:
            self._publish_error("media_bridge", f"Audio codec conversion unavailable for codec={codec}")
            return ""

        try:
            if codec == "g711_alaw":
                pcm = audioop.alaw2lin(audio_bytes, 2)
                ulaw = audioop.lin2ulaw(pcm, 2)
                return self._encode_base64_audio(ulaw)

            if codec == "pcm16":
                pcm_chunk = self._pcm16_leftover + audio_bytes
                if len(pcm_chunk) % 2 != 0:
                    self._pcm16_leftover = pcm_chunk[-1:]
                    pcm_chunk = pcm_chunk[:-1]
                else:
                    self._pcm16_leftover = b""
                if not pcm_chunk:
                    return ""
                if self._output_pcm_rate_hz != 8000:
                    pcm_chunk, self._pcm_ratecv_state = audioop.ratecv(
                        pcm_chunk,
                        2,
                        1,
                        self._output_pcm_rate_hz,
                        8000,
                        self._pcm_ratecv_state,
                    )
                ulaw = audioop.lin2ulaw(pcm_chunk, 2)
                return self._encode_base64_audio(ulaw)
        except Exception as exc:
            category, detail = classify_openai_error(str(exc))
            self._publish_error(category, detail)
            return ""

        return self._encode_base64_audio(audio_bytes)

    async def _send_opening_response(self, openai_ws) -> None:
        if self._opening_response_sent:
            return
        self._opening_response_sent = True
        await self._request_audio_response(
            openai_ws,
            instructions=(
                "Start the call now with a short greeting, identify yourself clearly, "
                "and ask one concise qualifying question."
            ),
        )

    async def _request_audio_response(self, openai_ws, instructions: str | None = None) -> None:
        response_payload: dict[str, Any] = {"modalities": ["audio"]}
        if instructions:
            response_payload["instructions"] = instructions
        await openai_ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": response_payload,
                }
            )
        )
        self._awaiting_audio = True
        self._last_response_request_at = time.monotonic()

    def _maybe_retry_silent_response(self, openai_ws) -> None:
        if not self._awaiting_audio:
            return
        elapsed = time.monotonic() - self._last_response_request_at
        if elapsed < 2.5:
            return
        if self._response_retry_count >= 2:
            if not self._voice_fallback_attempted:
                self._voice_fallback_attempted = True
                self._response_retry_count = 0
                asyncio.create_task(self._apply_voice_fallback_and_retry(openai_ws))
                return
            self._awaiting_audio = False
            self._publish_error("media_bridge", "OpenAI realtime produced no audio after retries (including voice fallback)")
            return
        self._response_retry_count += 1
        asyncio.create_task(
            self._request_audio_response(
                openai_ws,
                instructions="Respond now with one short spoken sentence.",
            )
        )

    async def _apply_voice_fallback_and_retry(self, openai_ws) -> None:
        try:
            await openai_ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {"voice": "alloy"},
                    }
                )
            )
            await self._request_audio_response(
                openai_ws,
                instructions="Speak now with a short greeting.",
            )
            self._publish_error("media_bridge", "No audio with configured voice; retried with fallback voice=alloy")
        except Exception as exc:
            category, detail = classify_openai_error(str(exc))
            self._publish_error(category, detail)

    def _consume_bus_events(self) -> None:
        def stop_when() -> bool:
            return self._stop_event.is_set()

        def on_event(event: AIRealtimeEvent) -> None:
            if event.event == "media.inbound":
                payload = str((event.payload or {}).get("audio") or "")
                if not payload:
                    return
                self._inbound_audio_queue.put(payload)
            elif event.event == "session.ended":
                self._stop_event.set()

        try:
            self.bus.consume(
                self.campaign_number_id,
                stop_when=stop_when,
                on_event=on_event,
            )
        except Exception as exc:
            category, detail = classify_openai_error(str(exc))
            self._publish_error(category, detail)
            self._stop_event.set()

    async def _flush_inbound_audio(self, openai_ws) -> None:
        while True:
            try:
                payload = self._inbound_audio_queue.get_nowait()
            except Empty:
                return
            try:
                await openai_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))
            except Exception as exc:
                category, detail = classify_openai_error(str(exc))
                self._publish_error(category, detail)
                self._stop_event.set()
                return

    async def _handle_openai_message(self, openai_ws, raw_message: str) -> None:
        data = json.loads(raw_message)
        event_type = str(data.get("type") or "")
        if not event_type:
            return

        if event_type in {"session.updated", "session.created"}:
            self._set_output_audio_format(dict(data.get("session") or {}))
            # Safety net: make sure we still trigger the first turn if session acknowledged later.
            await self._send_opening_response(openai_ws)
            return

        if event_type in {"response.audio.delta", "response.output_audio.delta"}:
            delta = str(data.get("delta") or "")
            if delta:
                outbound_audio = self._normalize_outbound_audio_for_twilio(delta)
                if not outbound_audio:
                    return
                self.bus.publish(self.campaign_number_id, "media.outbound", {"audio": outbound_audio})
                self._awaiting_audio = False
                self._response_retry_count = 0
                self._last_audio_out_at = time.monotonic()
            return

        if event_type in {
            "response.audio_transcript.delta",
            "response.text.delta",
            "response.output_text.delta",
        }:
            delta_text = str(data.get("delta") or "")
            if delta_text:
                self.bus.publish(self.campaign_number_id, "transcript.partial", {"text": delta_text})
            return

        if event_type in {
            "response.audio_transcript.done",
            "response.text.done",
            "response.output_text.done",
        }:
            final_text = str(
                data.get("text")
                or data.get("transcript")
                or ((data.get("output_text") or [None])[0] if isinstance(data.get("output_text"), list) else "")
                or ""
            )
            if final_text:
                self.bus.publish(self.campaign_number_id, "transcript.final", {"text": final_text})
                self.bus.publish(self.campaign_number_id, "assistant.response", {"text": final_text})
                self.sessions.increment_turn_count(self.campaign_number_id)
            return

        if event_type == "input_audio_buffer.speech_stopped":
            # Ensure the model generates a response after user speech.
            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await self._request_audio_response(openai_ws)
            return

        if event_type == "response.function_call_arguments.done":
            name = str(data.get("name") or "")
            if name != "transfer_call":
                return
            try:
                arguments = json.loads(data.get("arguments") or "{}")
            except Exception:
                arguments = {}
            reason = str(arguments.get("reason") or "Lead asked for a human").strip()
            self.sessions.update_session(
                self.campaign_number_id,
                handoff_reason=reason[:500],
                status="handoff_requested",
            )
            self.bus.publish(
                self.campaign_number_id,
                "tool.transfer_call",
                {"reason": reason[:500]},
            )
            return

        if event_type == "error":
            err = data.get("error") or {}
            message = str(err.get("message") or data)
            category, detail = classify_openai_error(message)
            self._publish_error(category, detail)
            return

        if event_type == "response.done":
            response = data.get("response") or {}
            status = str(response.get("status") or "").strip().lower()
            if status in {"failed", "cancelled", "incomplete"}:
                details = response.get("status_details") or {}
                reason = str(details.get("reason") or details.get("error") or response or "response failed")
                category, detail = classify_openai_error(reason)
                self._publish_error(category, detail)

    def _publish_error(self, category: str, detail: str) -> None:
        self.sessions.mark_error(self.campaign_number_id, category=category, detail=detail)
        self.bus.publish(
            self.campaign_number_id,
            "session.error",
            {"category": category, "detail": detail[:500]},
        )
