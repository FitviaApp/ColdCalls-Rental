"""
Worker-side bridge between SignalWire media events and OpenAI Realtime API.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any
from urllib.parse import urlencode
from queue import Queue, Empty

import websockets

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

            import asyncio

            while not self._stop_event.is_set():
                self._flush_inbound_audio(openai_ws)
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

    def _flush_inbound_audio(self, openai_ws) -> None:
        while True:
            try:
                payload = self._inbound_audio_queue.get_nowait()
            except Empty:
                return
            try:
                import asyncio

                asyncio.create_task(
                    openai_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))
                )
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

        if event_type == "response.audio.delta":
            delta = str(data.get("delta") or "")
            if delta:
                self.bus.publish(self.campaign_number_id, "media.outbound", {"audio": delta})
            return

        if event_type in {"response.audio_transcript.delta", "response.text.delta"}:
            delta_text = str(data.get("delta") or "")
            if delta_text:
                self.bus.publish(self.campaign_number_id, "transcript.partial", {"text": delta_text})
            return

        if event_type in {"response.audio_transcript.done", "response.text.done"}:
            final_text = str(data.get("text") or data.get("transcript") or "")
            if final_text:
                self.bus.publish(self.campaign_number_id, "transcript.final", {"text": final_text})
                self.bus.publish(self.campaign_number_id, "assistant.response", {"text": final_text})
                self.sessions.increment_turn_count(self.campaign_number_id)
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

    def _publish_error(self, category: str, detail: str) -> None:
        self.sessions.mark_error(self.campaign_number_id, category=category, detail=detail)
        self.bus.publish(
            self.campaign_number_id,
            "session.error",
            {"category": category, "detail": detail[:500]},
        )
