"""
Conversational AI campaign runtime using SignalWire, OpenAI, and ElevenLabs.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import AIAgent, CampaignNumber, CampaignMode
from app.services.signalwire_service import SignalWireService
from app.services.user_elevenlabs_service import get_user_elevenlabs_credentials
from app.services.user_openai_service import get_user_openai_credentials
from app.services.user_signalwire_service import get_user_signalwire_credentials

logger = logging.getLogger(__name__)
settings = get_settings()

AI_RUNTIME_DIR = Path(settings.AI_RUNTIME_DIR or "/tmp/coldcalls_ai_runtime")
AI_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


class CallTurnDeadlineError(RuntimeError):
    """Raised when the overall TwiML turn budget is exceeded."""


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp_path = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))

MAX_AGENT_TURNS = settings.AI_MAX_AGENT_TURNS
MAX_HISTORY_MESSAGES = settings.AI_MAX_HISTORY_MESSAGES
MAX_NO_INPUT_TURNS = settings.AI_MAX_NO_INPUT_TURNS
MAX_ASSISTANT_TEXT_CHARS = settings.AI_MAX_ASSISTANT_TEXT_CHARS


class InvalidProviderResponseError(RuntimeError):
    """Raised when a provider responds successfully but with unusable content."""


def _truncate_text(value: str | None, limit: int = 500) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:limit] or None


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
        db.query(CampaignNumber).filter(
            CampaignNumber.id == campaign_number_id
        ).update(safe_updates, synchronize_session=False)
        db.commit()
    finally:
        db.close()


class AICallRuntimeService:
    """Drive AI-agent campaign calls over SignalWire."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id

        project_id, api_token, space_url = get_user_signalwire_credentials(db, user_id)
        openai_api_key, openai_org_id = get_user_openai_credentials(db, user_id)
        elevenlabs_api_key = get_user_elevenlabs_credentials(db, user_id)

        if not project_id or not api_token or not space_url:
            raise ValueError("SignalWire credentials not configured")
        if not openai_api_key:
            raise ValueError("OpenAI credentials not configured")
        if not elevenlabs_api_key:
            raise ValueError("ElevenLabs credentials not configured")

        self.signalwire_service = SignalWireService(
            project_id=project_id,
            api_token=api_token,
            space_url=space_url,
        )
        self.openai_api_key = openai_api_key
        self.openai_org_id = openai_org_id
        self.elevenlabs_api_key = elevenlabs_api_key

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
            raise ValueError("AI runtime requires campaign_number_id metadata")

        session_payload = self._create_runtime_session(
            campaign_number_id=campaign_number_id,
            transfer_number=transfer_number,
            from_number=from_number,
            to_number=to_number,
        )

        answer_url = (
            f"{settings.BASE_URL.rstrip('/')}/api/ai-runtime/twiml/{campaign_number_id}"
        )
        call_result = self.signalwire_service.make_call(
            to_number=to_number,
            from_number=from_number,
            audio_url=None,
            transfer_number=transfer_number,
            campaign_id=campaign_id,
            timeout=timeout,
            metadata=metadata,
            answer_url=answer_url,
            enable_machine_detection=False,
        )

        session_payload["call_sid"] = call_result.get("call_sid")
        self._write_session(campaign_number_id, session_payload)
        return call_result

    def poll_call_status(self, *args, **kwargs):
        return self.signalwire_service.poll_call_status(*args, **kwargs)

    def build_initial_twiml(self, campaign_number_id: int) -> str:
        session = self._read_session(campaign_number_id)
        if not session:
            return self._hangup_twiml()

        current_turn = session.get("current_turn") or {}
        return self._twiml_for_turn(campaign_number_id, session, current_turn)

    def build_followup_twiml(
        self,
        campaign_number_id: int,
        *,
        user_input: str,
    ) -> str:
        session = self._read_session(campaign_number_id)
        if not session:
            return self._hangup_twiml()

        deadline = time.monotonic() + float(settings.AI_TURN_DEADLINE_SECONDS)

        normalized_user_input = (user_input or "").strip()
        if normalized_user_input:
            session["no_input_turns"] = 0
            session.setdefault("history", []).append(
                {"role": "user", "content": normalized_user_input}
            )
            update_campaign_number_ai_observability(
                campaign_number_id,
                ai_last_user_input=normalized_user_input,
                ai_no_input_turns=0,
            )
        else:
            session["no_input_turns"] = int(session.get("no_input_turns") or 0) + 1
            update_campaign_number_ai_observability(
                campaign_number_id,
                ai_no_input_turns=session["no_input_turns"],
            )
            if session["no_input_turns"] > MAX_NO_INPUT_TURNS:
                return self._safe_finalize_turn(
                    campaign_number_id,
                    session,
                    assistant_text="I could not hear you, so I will end the call now. Thank you and goodbye.",
                    should_transfer=False,
                    deadline=deadline,
                )
            return self._safe_finalize_turn(
                campaign_number_id,
                session,
                assistant_text="I didn't catch that. Are you still there?",
                should_transfer=False,
                deadline=deadline,
            )

        session["turn_count"] = int(session.get("turn_count") or 0) + 1
        if session["turn_count"] > MAX_AGENT_TURNS:
            return self._safe_finalize_turn(
                campaign_number_id,
                session,
                assistant_text="Thank you for your time. We'll follow up later. Goodbye.",
                should_transfer=False,
                deadline=deadline,
            )

        try:
            next_turn = self._generate_assistant_turn(session, deadline=deadline)
        except CallTurnDeadlineError:
            logger.warning(
                "AI turn deadline exceeded for campaign_number_id=%s — using spoken stall",
                campaign_number_id,
            )
            update_campaign_number_ai_observability(
                campaign_number_id,
                ai_runtime_error="[timeout] AI turn deadline exceeded — played stall prompt",
            )
            return self._build_redirect_twiml(
                campaign_number_id,
                say_text="One moment please while I pull that up.",
            )
        except Exception as exc:
            logger.error(
                "AI turn generation failed for campaign_number_id=%s: %s",
                campaign_number_id,
                exc,
            )
            update_campaign_number_ai_observability(
                campaign_number_id,
                ai_runtime_error=str(exc)[:500],
            )
            return self._build_redirect_twiml(
                campaign_number_id,
                say_text="One moment please.",
            )

        session["current_turn"] = next_turn
        self._write_session(campaign_number_id, session)
        return self._twiml_for_turn(campaign_number_id, session, next_turn)

    def _safe_finalize_turn(
        self,
        campaign_number_id: int,
        session: dict[str, Any],
        *,
        assistant_text: str,
        should_transfer: bool,
        deadline: float,
    ) -> str:
        try:
            turn = self._create_assistant_turn(
                session,
                assistant_text=assistant_text,
                should_transfer=should_transfer,
                deadline=deadline,
            )
        except CallTurnDeadlineError:
            return self._say_then_hangup_twiml(assistant_text)
        except Exception as exc:
            logger.error(
                "Assistant turn finalize failed for campaign_number_id=%s: %s",
                campaign_number_id,
                exc,
            )
            update_campaign_number_ai_observability(
                campaign_number_id,
                ai_runtime_error=str(exc)[:500],
            )
            return self._say_then_hangup_twiml(assistant_text)

        session["current_turn"] = turn
        self._write_session(campaign_number_id, session)
        return self._twiml_for_turn(campaign_number_id, session, turn)

    def _create_runtime_session(
        self,
        *,
        campaign_number_id: int,
        transfer_number: str,
        from_number: str,
        to_number: str,
    ) -> dict[str, Any]:
        db = self.db
        number = db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).first()
        if not number or not number.campaign:
            raise ValueError("Campaign number not found for AI runtime")

        campaign = number.campaign
        if campaign.campaign_mode != CampaignMode.AI_AGENT:
            raise ValueError("Campaign is not in AI agent mode")
        agent = campaign.ai_agent
        if not agent or not agent.is_active:
            raise ValueError("AI agent is not available")

        session_payload: dict[str, Any] = {
            "campaign_number_id": campaign_number_id,
            "campaign_id": campaign.id,
            "user_id": campaign.user_id,
            "ai_agent_id": agent.id,
            "transfer_number": transfer_number,
            "from_number": from_number,
            "to_number": to_number,
            "turn_count": 0,
            "no_input_turns": 0,
            "history": [],
            "current_turn": None,
        }
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_turn_count=0,
            ai_no_input_turns=0,
            ai_last_user_input=None,
            ai_last_assistant_text=None,
            ai_handoff_reason=None,
            ai_runtime_error=None,
        )

        first_turn = self._generate_assistant_turn(session_payload, agent=agent)
        session_payload["current_turn"] = first_turn
        self._write_session(campaign_number_id, session_payload)
        return session_payload

    def _generate_assistant_turn(
        self,
        session_payload: dict[str, Any],
        *,
        agent: Optional[AIAgent] = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        db = self.db
        if agent is None:
            agent_id = int(session_payload.get("ai_agent_id") or 0)
            agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        if not agent:
            raise ValueError("AI agent not found")

        reply = self._request_openai_turn(
            agent, session_payload.get("history") or [], deadline=deadline
        )
        assistant_text = self._sanitize_assistant_text(
            reply.get("assistant_text") or "Hello, this is a quick follow-up call."
        )
        should_transfer = bool(reply.get("should_transfer"))
        handoff_reason = _truncate_text(reply.get("handoff_reason"), 500)
        if should_transfer and "connect" not in assistant_text.lower():
            assistant_text = f"{assistant_text.rstrip()} Please hold while I connect you now."

        return self._create_assistant_turn(
            session_payload,
            assistant_text=assistant_text,
            should_transfer=should_transfer,
            handoff_reason=handoff_reason,
            deadline=deadline,
        )

    def _create_assistant_turn(
        self,
        session_payload: dict[str, Any],
        *,
        assistant_text: str,
        should_transfer: bool,
        handoff_reason: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        campaign_number_id = int(session_payload["campaign_number_id"])
        audio_token = secrets.token_hex(8)
        audio_path = self._audio_path(campaign_number_id, audio_token)
        sanitized_text = self._sanitize_assistant_text(assistant_text)
        audio_bytes = self._synthesize_text_to_speech(
            sanitized_text,
            voice_id=self._agent_voice_id(session_payload),
            deadline=deadline,
        )
        _atomic_write_bytes(audio_path, audio_bytes)
        session_payload.setdefault("history", []).append(
            {"role": "assistant", "content": sanitized_text}
        )
        turn_count = int(session_payload.get("turn_count") or 0)
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_turn_count=turn_count,
            ai_last_assistant_text=sanitized_text,
            ai_handoff_reason=handoff_reason if should_transfer else None,
            ai_runtime_error=None,
        )
        return {
            "assistant_text": sanitized_text,
            "should_transfer": should_transfer,
            "audio_token": audio_token,
        }

    def _request_openai_turn(
        self,
        agent: AIAgent,
        history: list[dict[str, str]],
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        system_prompt = (
            f"You are an outbound phone agent speaking only in English. "
            f"Your job is to pre-qualify the lead, keep replies concise for voice, "
            f"and call the transfer_call tool when the lead is qualified or explicitly asks for a human. "
            f"Agent instructions: {agent.system_prompt.strip()} "
            f"Handoff guidance: {(agent.handoff_description or 'Transfer when the lead is ready for a human.').strip()}"
        )
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend((history or [])[-MAX_HISTORY_MESSAGES:])

        payload = {
            "model": agent.model or settings.OPENAI_DEFAULT_MODEL,
            "temperature": float(agent.temperature or 0.7),
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "transfer_call",
                        "description": "Transfer the call to the human agent when the lead is qualified or asks for a person.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "reason": {"type": "string"}
                            },
                            "required": ["reason"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        }

        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json",
        }
        if self.openai_org_id:
            headers["OpenAI-Organization"] = self.openai_org_id

        data = self._post_json_with_retries(
            provider_name="OpenAI",
            url=f"{settings.OPENAI_API_BASE.rstrip('/')}/chat/completions",
            headers=headers,
            json_payload=payload,
            timeout_seconds=float(settings.OPENAI_REQUEST_TIMEOUT_SECONDS),
            deadline=deadline,
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
        assistant_text = str(message.get("content") or "").strip()
        if not assistant_text and should_transfer:
            assistant_text = "Thanks, I can connect you with a specialist now."
        if not assistant_text:
            assistant_text = "Hello, this is a quick follow-up call."
        return {
            "assistant_text": assistant_text,
            "should_transfer": should_transfer,
            "handoff_reason": handoff_reason,
        }

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

    def _synthesize_text_to_speech(
        self,
        text: str,
        *,
        voice_id: str,
        deadline: float | None = None,
    ) -> bytes:
        payload = {
            "text": text,
            "model_id": settings.ELEVENLABS_TTS_MODEL,
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.75,
            },
        }
        headers = {
            "xi-api-key": self.elevenlabs_api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        return self._post_binary_with_retries(
            provider_name="ElevenLabs",
            url=f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers=headers,
            json_payload=payload,
            timeout_seconds=float(settings.ELEVENLABS_REQUEST_TIMEOUT_SECONDS),
            deadline=deadline,
        )

    def _post_json_with_retries(
        self,
        *,
        provider_name: str,
        url: str,
        headers: dict[str, str],
        json_payload: dict[str, Any],
        timeout_seconds: float,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        response = self._post_with_retries(
            provider_name=provider_name,
            url=url,
            headers=headers,
            json_payload=json_payload,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
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
        deadline: float | None = None,
    ) -> bytes:
        response = self._post_with_retries(
            provider_name=provider_name,
            url=url,
            headers=headers,
            json_payload=json_payload,
            timeout_seconds=timeout_seconds,
            response_validator=self._validate_audio_response,
            deadline=deadline,
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
        deadline: float | None = None,
        response_validator: Callable[[httpx.Response], None] | None = None,
    ) -> httpx.Response:
        max_attempts = max(1, int(settings.AI_HTTP_MAX_RETRIES) + 1)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            effective_timeout = timeout_seconds
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0.5:
                    raise CallTurnDeadlineError(
                        f"{provider_name} request aborted: turn deadline exceeded"
                    )
                # Never outrun the deadline, but give the provider at least 1s.
                effective_timeout = max(1.0, min(timeout_seconds, remaining - 0.25))

            try:
                with httpx.Client(timeout=effective_timeout) as client:
                    response = client.post(
                        url,
                        headers=headers,
                        json=json_payload,
                    )
                response.raise_for_status()
                if response_validator is not None:
                    response_validator(response)
                return response
            except (
                httpx.TimeoutException,
                httpx.RequestError,
                httpx.HTTPStatusError,
                InvalidProviderResponseError,
            ) as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                backoff_seconds = float(settings.AI_HTTP_RETRY_BACKOFF_SECONDS) * attempt
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= backoff_seconds + 1.0:
                        break
                logger.warning(
                    "%s request attempt %s/%s failed: %s. Retrying in %.2fs",
                    provider_name,
                    attempt,
                    max_attempts,
                    exc,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)

        raise RuntimeError(
            f"{provider_name} request failed after {max_attempts} attempt(s): {last_error}"
        )

    def _validate_audio_response(self, response: httpx.Response) -> None:
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not response.content:
            raise InvalidProviderResponseError("provider returned an empty audio response")
        if content_type and not content_type.startswith("audio/"):
            body_preview = _truncate_text(response.text, 160) or "no response body"
            raise InvalidProviderResponseError(
                f"provider returned {content_type} instead of audio: {body_preview}"
            )

    def _agent_voice_id(self, session_payload: dict[str, Any]) -> str:
        agent_id = int(session_payload.get("ai_agent_id") or 0)
        agent = self.db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        if not agent or not agent.voice_id:
            raise ValueError("AI agent voice is not configured")
        return agent.voice_id

    def _twiml_for_turn(
        self,
        campaign_number_id: int,
        session_payload: dict[str, Any],
        current_turn: dict[str, Any],
    ) -> str:
        audio_url = (
            f"{settings.BASE_URL.rstrip('/')}/api/ai-runtime/audio/"
            f"{campaign_number_id}/{current_turn['audio_token']}"
        )
        if current_turn.get("should_transfer"):
            return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
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

        gather_action = (
            f"{settings.BASE_URL.rstrip('/')}/api/ai-runtime/twiml/{campaign_number_id}/gather"
        )
        # Outer Gather listens *during* playback too, so interrupting the agent
        # works. A trailing Redirect keeps the call alive if the first Gather
        # expires silently — it retries rather than hanging up immediately.
        redirect_url = gather_action
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech dtmf" speechTimeout="{speech_timeout_seconds}" timeout="{gather_timeout_seconds}" action="{gather_action}" method="POST" actionOnEmptyResult="true">
        <Play>{audio_url}</Play>{pause_block}
    </Gather>
    <Redirect method="POST">{redirect_url}</Redirect>
</Response>"""

    def _hangup_twiml(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    def _build_redirect_twiml(self, campaign_number_id: int, *, say_text: str) -> str:
        """Stall for time without hanging up — say a short line and loop back."""
        safe_text = self._xml_escape(say_text)
        gather_action = (
            f"{settings.BASE_URL.rstrip('/')}/api/ai-runtime/twiml/{campaign_number_id}/gather"
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">{safe_text}</Say>
    <Redirect method="POST">{gather_action}</Redirect>
</Response>"""

    def _say_then_hangup_twiml(self, say_text: str) -> str:
        safe_text = self._xml_escape(say_text)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">{safe_text}</Say>
    <Hangup/>
</Response>"""

    @staticmethod
    def _xml_escape(value: str) -> str:
        return (
            str(value or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    def _session_path(self, campaign_number_id: int) -> Path:
        return AI_RUNTIME_DIR / f"{campaign_number_id}.json"

    def _audio_path(self, campaign_number_id: int, audio_token: str) -> Path:
        return AI_RUNTIME_DIR / f"{campaign_number_id}-{audio_token}.mp3"

    def _read_session(self, campaign_number_id: int) -> Optional[dict[str, Any]]:
        path = self._session_path(campaign_number_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "AI runtime session read failed for campaign_number_id=%s: %s",
                campaign_number_id,
                exc,
            )
            return None

    def _write_session(self, campaign_number_id: int, payload: dict[str, Any]) -> None:
        _atomic_write_text(self._session_path(campaign_number_id), json.dumps(payload))


def build_ai_runtime_twiml(campaign_number_id: int) -> str:
    db = SessionLocal()
    try:
        number = db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).first()
        if not number or not number.campaign:
            return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
        return AICallRuntimeService(db, number.campaign.user_id).build_initial_twiml(campaign_number_id)
    except Exception as exc:
        logger.error(f"AI runtime initial TwiML failed for number {campaign_number_id}: {exc}")
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_runtime_error=str(exc),
        )
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    finally:
        db.close()


def build_ai_runtime_followup_twiml(campaign_number_id: int, user_input: str) -> str:
    db = SessionLocal()
    try:
        number = db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).first()
        if not number or not number.campaign:
            return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
        return AICallRuntimeService(db, number.campaign.user_id).build_followup_twiml(
            campaign_number_id,
            user_input=user_input,
        )
    except Exception as exc:
        logger.error(f"AI runtime follow-up TwiML failed for number {campaign_number_id}: {exc}")
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_runtime_error=str(exc),
        )
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    finally:
        db.close()


def get_ai_runtime_audio(campaign_number_id: int, audio_token: str) -> Optional[bytes]:
    audio_path = AI_RUNTIME_DIR / f"{campaign_number_id}-{audio_token}.mp3"
    if not audio_path.exists():
        return None
    return audio_path.read_bytes()


def cleanup_ai_runtime_artifacts(campaign_number_id: int) -> None:
    """Remove persisted AI runtime session/audio files for a campaign number."""
    session_path = AI_RUNTIME_DIR / f"{campaign_number_id}.json"
    if session_path.exists():
        session_path.unlink(missing_ok=True)

    for audio_path in AI_RUNTIME_DIR.glob(f"{campaign_number_id}-*.mp3"):
        audio_path.unlink(missing_ok=True)


def prune_stale_ai_runtime_artifacts(max_age_seconds: int | None = None) -> int:
    """Delete stale AI runtime files and return the number of removed artifacts."""
    ttl_seconds = int(max_age_seconds or settings.AI_RUNTIME_ARTIFACT_TTL_SECONDS)
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
