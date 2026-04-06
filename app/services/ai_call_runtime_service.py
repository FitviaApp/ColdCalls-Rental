"""
AI Realtime runtime using SignalWire media stream + OpenAI Realtime.
"""
from __future__ import annotations

import logging
import threading
import time
import json
from pathlib import Path
from typing import Any, Optional, Callable
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import AIAgent, CampaignNumber, CampaignMode
from app.services.ai_realtime_bridge_worker import AIRealtimeBridgeWorker
from app.services.ai_realtime_event_bus import AIRealtimeEventBus
from app.services.ai_realtime_session_service import AIRealtimeSessionService
from app.services.signalwire_service import SignalWireService
from app.services.twilio_service import TwilioService
from app.services.user_openai_service import get_user_openai_credentials
from app.services.user_signalwire_service import get_user_signalwire_credentials
from app.services.user_twilio_service import get_user_twilio_credentials

logger = logging.getLogger(__name__)
settings = get_settings()

AI_RUNTIME_DIR = Path("/tmp/coldcalls_ai_runtime")
AI_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
MAX_AGENT_TURNS = settings.AI_MAX_AGENT_TURNS
MAX_HISTORY_MESSAGES = settings.AI_MAX_HISTORY_MESSAGES
MAX_NO_INPUT_TURNS = settings.AI_MAX_NO_INPUT_TURNS
MAX_ASSISTANT_TEXT_CHARS = settings.AI_MAX_ASSISTANT_TEXT_CHARS

_bridge_threads: dict[int, tuple[AIRealtimeBridgeWorker, threading.Thread]] = {}


def _truncate_text(value: str | None, limit: int = 500) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:limit] or None


def _ws_base_from_public_url(base_url: str) -> str:
    normalized = str(base_url or "").strip()
    if normalized.startswith("https://"):
        return "wss://" + normalized[len("https://"):]
    if normalized.startswith("http://"):
        return "ws://" + normalized[len("http://"):]
    return "wss://" + normalized.lstrip("/")


def update_campaign_number_ai_observability(campaign_number_id: int, **updates) -> None:
    safe_updates = {
        key: _truncate_text(value, 1000) if isinstance(value, str) else value
        for key, value in updates.items()
        if value is not None or key in {
            "ai_last_user_input",
            "ai_last_assistant_text",
            "ai_handoff_reason",
            "ai_runtime_error",
        }
    }
    if not safe_updates:
        return
    db = SessionLocal()
    try:
        db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).update(
            safe_updates,
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()


class AICallRuntimeService:
    """AI-agent runtime facade used by campaign worker."""

    def __init__(self, db: Session, user_id: int, provider: str = "signalwire"):
        self.db = db
        self.user_id = user_id
        self.provider = (provider or "signalwire").strip().lower()
        openai_api_key, openai_org_id = get_user_openai_credentials(db, user_id)
        if not openai_api_key:
            raise ValueError("OpenAI credentials not configured")

        if self.provider == "signalwire":
            project_id, api_token, space_url = get_user_signalwire_credentials(db, user_id)
            if not project_id or not api_token or not space_url:
                raise ValueError("SignalWire credentials not configured")
            self.call_service = SignalWireService(
                project_id=project_id,
                api_token=api_token,
                space_url=space_url,
            )
        elif self.provider == "twilio":
            account_sid, auth_token = get_user_twilio_credentials(db, user_id)
            if not account_sid or not auth_token:
                raise ValueError("Twilio credentials not configured")
            self.call_service = TwilioService(account_sid=account_sid, auth_token=auth_token)
        else:
            raise ValueError(f"Unsupported AI runtime provider: {self.provider}")

        self.openai_api_key = openai_api_key
        self.openai_org_id = openai_org_id
        self.session_service = AIRealtimeSessionService()
        self.bus = AIRealtimeEventBus()

    def make_call(
        self,
        to_number: str,
        from_number: str,
        audio_url: Optional[str],
        transfer_number: str,
        campaign_id: Optional[int] = None,
        press_1_to_talk_with_agent: bool = False,
        timeout: int = 60,
        metadata: Optional[dict] = None,
    ) -> dict:
        del audio_url, press_1_to_talk_with_agent
        metadata = metadata or {}
        campaign_number_id = int(metadata.get("campaign_number_id") or 0)
        if not campaign_number_id:
            raise ValueError("AI realtime runtime requires campaign_number_id metadata")

        session_payload, agent = self._create_runtime_session(
            campaign_number_id=campaign_number_id,
            transfer_number=transfer_number,
            from_number=from_number,
            to_number=to_number,
            campaign_id=campaign_id,
        )

        query = urlencode({"token": session_payload["auth_token"]})
        answer_url = f"{settings.BASE_URL.rstrip('/')}/api/ai-realtime/twiml/{campaign_number_id}?{query}"
        make_call_kwargs: dict[str, Any] = {
            "to_number": to_number,
            "from_number": from_number,
            "audio_url": None,
            "transfer_number": transfer_number,
            "campaign_id": campaign_id,
            "timeout": timeout,
            "metadata": metadata,
        }
        make_call_kwargs["answer_url"] = answer_url
        make_call_kwargs["enable_machine_detection"] = False
        call_result = self.call_service.make_call(**make_call_kwargs)
        call_sid = str(call_result.get("call_sid") or "")
        self.session_service.set_call_sid(campaign_number_id, call_sid)
        self.bus.publish(campaign_number_id, "session.started", {"call_sid": call_sid})
        self._start_bridge_thread(campaign_number_id, agent)
        return call_result

    def poll_call_status(self, *args, **kwargs):
        return self.call_service.poll_call_status(*args, **kwargs)

    def update_call_twiml(self, call_sid: str, twiml: str) -> None:
        self.call_service.update_call_twiml(call_sid, twiml)

    def _create_runtime_session(
        self,
        *,
        campaign_number_id: int,
        transfer_number: str,
        from_number: str,
        to_number: str,
        campaign_id: int | None,
    ) -> tuple[dict[str, Any], AIAgent]:
        number = self.db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).first()
        if not number or not number.campaign:
            raise ValueError("Campaign number not found for AI realtime runtime")

        campaign = number.campaign
        if campaign.campaign_mode != CampaignMode.AI_AGENT:
            raise ValueError("Campaign is not in AI agent mode")
        campaign_provider = (
            campaign.voice_provider.value
            if hasattr(campaign.voice_provider, "value")
            else str(campaign.voice_provider)
        ).strip().lower()
        if campaign_provider != self.provider:
            raise ValueError(
                f"AI runtime provider mismatch: campaign={campaign_provider} runtime={self.provider}"
            )
        if campaign_id and int(campaign.id) != int(campaign_id):
            raise ValueError("Campaign mismatch for AI runtime session")
        agent = campaign.ai_agent
        if not agent or not agent.is_active:
            raise ValueError("AI agent is not available")

        session_payload = self.session_service.create_session(
            campaign_number_id=campaign_number_id,
            campaign_id=int(campaign.id),
            user_id=int(campaign.user_id),
            ai_agent_id=int(agent.id),
            from_number=from_number,
            to_number=to_number,
            transfer_number=transfer_number,
        )
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_turn_count=0,
            ai_no_input_turns=0,
            ai_last_user_input=None,
            ai_last_assistant_text=None,
            ai_handoff_reason=None,
            ai_runtime_error=None,
        )
        return session_payload, agent

    # -------- Legacy helper methods (kept for compatibility tests) --------
    def build_followup_twiml(self, campaign_number_id: int, *, user_input: str) -> str:
        session = self._read_session(campaign_number_id)
        if not session:
            return self._hangup_twiml()
        normalized_user_input = (user_input or "").strip()
        if normalized_user_input:
            session["no_input_turns"] = 0
            session.setdefault("history", []).append({"role": "user", "content": normalized_user_input})
        else:
            session["no_input_turns"] = int(session.get("no_input_turns") or 0) + 1
            reprompt_turn = self._create_assistant_turn(
                session,
                assistant_text="I did not catch that. Are you still there?",
                should_transfer=False,
            )
            session["current_turn"] = reprompt_turn
            self._write_session(campaign_number_id, session)
            return self._twiml_for_turn(campaign_number_id, session, reprompt_turn)
        return self._hangup_twiml()

    def _sanitize_assistant_text(self, text: str) -> str:
        normalized = " ".join(str(text or "").split())
        if not normalized:
            return "Hello, this is a quick follow-up call."
        if len(normalized) <= MAX_ASSISTANT_TEXT_CHARS:
            return normalized
        trimmed = normalized[:MAX_ASSISTANT_TEXT_CHARS].rstrip()
        last_sentence_break = max(trimmed.rfind("."), trimmed.rfind("?"), trimmed.rfind("!"))
        if last_sentence_break >= 40:
            return trimmed[: last_sentence_break + 1]
        last_space = trimmed.rfind(" ")
        if last_space >= 40:
            return trimmed[:last_space].rstrip() + "..."
        return trimmed + "..."

    def _request_openai_turn(self, agent: AIAgent, history: list[dict[str, str]]) -> dict[str, Any]:
        payload = {
            "model": agent.model or settings.OPENAI_DEFAULT_MODEL,
            "temperature": float(agent.temperature or 0.7),
            "messages": [{"role": "system", "content": agent.system_prompt}] + (history or [])[-MAX_HISTORY_MESSAGES:],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "transfer_call",
                        "description": "Transfer call",
                        "parameters": {
                            "type": "object",
                            "properties": {"reason": {"type": "string"}},
                            "required": ["reason"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        }
        headers = {"Authorization": f"Bearer {self.openai_api_key}", "Content-Type": "application/json"}
        if self.openai_org_id:
            headers["OpenAI-Organization"] = self.openai_org_id
        data = self._post_json_with_retries(
            provider_name="OpenAI",
            url=f"{settings.OPENAI_API_BASE.rstrip('/')}/chat/completions",
            headers=headers,
            json_payload=payload,
            timeout_seconds=float(settings.OPENAI_REQUEST_TIMEOUT_SECONDS),
        )
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        should_transfer = False
        handoff_reason = None
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            if function.get("name") != "transfer_call":
                continue
            should_transfer = True
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except Exception:
                arguments = {}
            handoff_reason = _truncate_text(arguments.get("reason"), 500)
            break
        assistant_text = str(message.get("content") or "").strip() or "Hello, this is a quick follow-up call."
        return {
            "assistant_text": assistant_text,
            "should_transfer": should_transfer,
            "handoff_reason": handoff_reason,
        }

    def _post_json_with_retries(
        self,
        *,
        provider_name: str,
        url: str,
        headers: dict[str, str],
        json_payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        response = self._post_with_retries(
            provider_name=provider_name,
            url=url,
            headers=headers,
            json_payload=json_payload,
            timeout_seconds=timeout_seconds,
        )
        return response.json()

    def _post_binary_with_retries(
        self,
        *,
        provider_name: str,
        url: str,
        headers: dict[str, str],
        json_payload: dict[str, Any],
        timeout_seconds: float,
    ) -> bytes:
        response = self._post_with_retries(
            provider_name=provider_name,
            url=url,
            headers=headers,
            json_payload=json_payload,
            timeout_seconds=timeout_seconds,
            response_validator=self._validate_audio_response,
        )
        return response.content

    def _post_with_retries(
        self,
        *,
        provider_name: str,
        url: str,
        headers: dict[str, str],
        json_payload: dict[str, Any],
        timeout_seconds: float,
        response_validator: Callable[[httpx.Response], None] | None = None,
    ) -> httpx.Response:
        max_attempts = max(1, int(settings.AI_HTTP_MAX_RETRIES) + 1)
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=timeout_seconds) as client:
                    response = client.post(url, headers=headers, json=json_payload)
                response.raise_for_status()
                if response_validator is not None:
                    response_validator(response)
                return response
            except Exception as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                backoff_seconds = float(settings.AI_HTTP_RETRY_BACKOFF_SECONDS) * attempt
                time.sleep(backoff_seconds)
        raise RuntimeError(f"{provider_name} request failed after {max_attempts} attempt(s): {last_error}")

    def _validate_audio_response(self, response: httpx.Response) -> None:
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not response.content:
            raise RuntimeError("provider returned an empty audio response")
        if content_type and not content_type.startswith("audio/"):
            body_preview = _truncate_text(response.text, 160) or "no response body"
            raise RuntimeError(f"provider returned {content_type} instead of audio: {body_preview}")

    def _create_assistant_turn(
        self,
        session_payload: dict[str, Any],
        *,
        assistant_text: str,
        should_transfer: bool,
        handoff_reason: str | None = None,
    ) -> dict[str, Any]:
        _ = handoff_reason
        return {
            "assistant_text": self._sanitize_assistant_text(assistant_text),
            "should_transfer": bool(should_transfer),
            "audio_token": "legacy",
        }

    def _twiml_for_turn(
        self,
        campaign_number_id: int,
        session_payload: dict[str, Any],
        current_turn: dict[str, Any],
    ) -> str:
        _ = campaign_number_id
        if current_turn.get("should_transfer"):
            return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{session_payload['from_number']}" timeout="30">
        <Number>{session_payload['transfer_number']}</Number>
    </Dial>
</Response>"""
        gather_timeout_seconds = max(1, int(settings.AI_GATHER_TIMEOUT_SECONDS))
        speech_timeout_seconds = max(1, int(settings.AI_GATHER_SPEECH_TIMEOUT_SECONDS))
        post_play_pause_seconds = max(0, int(settings.AI_GATHER_POST_PLAY_PAUSE_SECONDS))
        pause_block = (
            f"\n        <Pause length=\"{post_play_pause_seconds}\"/>"
            if post_play_pause_seconds
            else ""
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech dtmf" speechTimeout="{speech_timeout_seconds}" timeout="{gather_timeout_seconds}" actionOnEmptyResult="true">{pause_block}
    </Gather>
    <Hangup/>
</Response>"""

    def _hangup_twiml(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    def _session_path(self, campaign_number_id: int) -> Path:
        return AI_RUNTIME_DIR / f"{campaign_number_id}.json"

    def _read_session(self, campaign_number_id: int) -> Optional[dict[str, Any]]:
        path = self._session_path(campaign_number_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _write_session(self, campaign_number_id: int, payload: dict[str, Any]) -> None:
        self._session_path(campaign_number_id).write_text(json.dumps(payload))

    def _start_bridge_thread(self, campaign_number_id: int, agent: AIAgent) -> None:
        existing = _bridge_threads.get(campaign_number_id)
        if existing and existing[1].is_alive():
            return

        bridge = AIRealtimeBridgeWorker(
            campaign_number_id=campaign_number_id,
            agent=agent,
            openai_api_key=self.openai_api_key,
            openai_org_id=self.openai_org_id,
        )
        thread = threading.Thread(
            target=bridge.run,
            daemon=True,
            name=f"ai-rt-bridge-{campaign_number_id}",
        )
        _bridge_threads[campaign_number_id] = (bridge, thread)
        thread.start()


def build_ai_runtime_twiml(campaign_number_id: int) -> str:
    """Legacy endpoint retained outside AI v2 path."""
    _ = campaign_number_id
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def build_ai_runtime_followup_twiml(campaign_number_id: int, user_input: str) -> str:
    """Legacy endpoint retained outside AI v2 path."""
    _ = campaign_number_id, user_input
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def build_ai_realtime_stream_twiml(campaign_number_id: int, token: str) -> str:
    ws_base = _ws_base_from_public_url(settings.BASE_URL.rstrip("/"))
    ws_url = f"{ws_base}/api/ai-realtime/ws/{campaign_number_id}?token={token}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""


def get_ai_runtime_audio(campaign_number_id: int, audio_token: str) -> Optional[bytes]:
    _ = campaign_number_id, audio_token
    return None


def cleanup_ai_runtime_artifacts(campaign_number_id: int) -> None:
    """Compatibility cleanup hook for worker finalization."""
    bridge_tuple = _bridge_threads.pop(campaign_number_id, None)
    if bridge_tuple:
        bridge, _thread = bridge_tuple
        bridge.stop()
    try:
        AIRealtimeSessionService().end_session(campaign_number_id, status="ended")
        AIRealtimeEventBus().publish(campaign_number_id, "session.ended", {"source": "cleanup"})
    except Exception as exc:
        logger.warning("Failed cleaning AI realtime session %s: %s", campaign_number_id, exc)
    session_path = AI_RUNTIME_DIR / f"{campaign_number_id}.json"
    if session_path.exists():
        session_path.unlink(missing_ok=True)
    for audio_path in AI_RUNTIME_DIR.glob(f"{campaign_number_id}-*.mp3"):
        audio_path.unlink(missing_ok=True)


def prune_stale_ai_runtime_artifacts(max_age_seconds: int | None = None) -> int:
    ttl_seconds = int(max_age_seconds or settings.AI_REALTIME_SESSION_TTL_SECONDS)
    if ttl_seconds <= 0:
        return 0
    cutoff_timestamp = time.time() - ttl_seconds
    removed = 0
    for artifact_path in AI_RUNTIME_DIR.glob("*"):
        try:
            if not artifact_path.is_file():
                continue
            if artifact_path.suffix not in {".json", ".mp3"}:
                continue
            if artifact_path.stat().st_mtime >= cutoff_timestamp:
                continue
            artifact_path.unlink(missing_ok=True)
            removed += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Failed pruning AI runtime artifact %s: %s", artifact_path, exc)
    return removed
