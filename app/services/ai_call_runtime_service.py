"""
AI runtime service for AI-agent campaigns.

Active path (legacy):
- Telephony answer URL -> /api/ai-runtime/twiml/{campaign_number_id}
- Turn-based loop (Gather -> OpenAI Chat -> ElevenLabs TTS -> Play/Gather)

Realtime helpers/endpoints are kept for compatibility but are no longer the
operational path for AI campaigns.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import AIAgent, CampaignMode, CampaignNumber
from app.services.signalwire_service import SignalWireService
from app.services.twilio_service import TwilioService
from app.services.user_elevenlabs_service import get_user_elevenlabs_credentials
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

# Legacy compatibility for previous realtime thread map.
_bridge_threads: dict[int, tuple[Any, threading.Thread]] = {}


def _truncate_text(value: str | None, limit: int = 500) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:limit] or None


def _ws_base_from_public_url(base_url: str) -> str:
    normalized = str(base_url or "").strip()
    if normalized.startswith("https://"):
        return "wss://" + normalized[len("https://"):]
    if normalized.startswith("http://"):
        return "wss://" + normalized[len("http://"):]
    return "wss://" + normalized.lstrip("/")


def _chat_model_for_agent(model: str | None) -> str:
    raw = (model or "").strip()
    if not raw:
        return settings.OPENAI_DEFAULT_MODEL
    lowered = raw.lower()
    if lowered.startswith("gpt-realtime") or lowered.startswith("gpt-4o-realtime"):
        return settings.OPENAI_DEFAULT_MODEL
    return raw


def classify_ai_runtime_error(raw_error: str) -> tuple[str, str]:
    value = str(raw_error or "").strip()
    lowered = value.lower()

    if "401" in lowered or "403" in lowered or "invalid api key" in lowered or "authentication" in lowered:
        return "provider_auth", value[:500]
    if "429" in lowered or "rate limit" in lowered:
        return "provider_rate_limit", value[:500]
    if "tool" in lowered:
        return "tooling", value[:500]
    if "policy" in lowered or "content_filter" in lowered or "safety" in lowered:
        return "policy", value[:500]
    return "media_bridge", value[:500]


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
        existing_columns: set[str] | None = None
        try:
            inspector = sa_inspect(db.get_bind())
            existing_columns = {
                str(column.get("name") or "").strip()
                for column in inspector.get_columns(CampaignNumber.__tablename__)
            }
        except Exception:
            existing_columns = None

        if existing_columns:
            safe_updates = {key: value for key, value in safe_updates.items() if key in existing_columns}
            if not safe_updates:
                return

        db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).update(
            safe_updates,
            synchronize_session=False,
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning(
            "Failed to persist AI observability fields for campaign_number_id=%s: %s",
            campaign_number_id,
            exc,
        )
    finally:
        db.close()


class AICallRuntimeService:
    """AI-agent runtime facade used by campaign worker."""

    def __init__(self, db: Session, user_id: int, provider: str = "signalwire"):
        self.db = db
        self.user_id = user_id
        self.provider = (provider or "signalwire").strip().lower()
        self.handoff_via_event_bus = False

        openai_api_key, openai_org_id = get_user_openai_credentials(db, user_id)
        if not openai_api_key:
            raise ValueError("OpenAI credentials not configured")

        elevenlabs_api_key = get_user_elevenlabs_credentials(db, user_id)
        if not elevenlabs_api_key:
            raise ValueError("ElevenLabs credentials not configured")

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

        self._create_runtime_session(
            campaign_number_id=campaign_number_id,
            transfer_number=transfer_number,
            from_number=from_number,
            to_number=to_number,
            campaign_id=campaign_id,
        )

        answer_url = f"{settings.BASE_URL.rstrip('/')}/api/ai-runtime/twiml/{campaign_number_id}"
        make_call_kwargs: dict[str, Any] = {
            "to_number": to_number,
            "from_number": from_number,
            "audio_url": None,
            "transfer_number": transfer_number,
            "campaign_id": campaign_id,
            "timeout": timeout,
            "metadata": metadata,
            "answer_url": answer_url,
            "enable_machine_detection": False,
        }
        return self.call_service.make_call(**make_call_kwargs)

    def poll_call_status(self, *args, **kwargs):
        return self.call_service.poll_call_status(*args, **kwargs)

    def update_call_twiml(self, call_sid: str, twiml: str) -> None:
        self.call_service.update_call_twiml(call_sid, twiml)

    def build_initial_twiml(self, campaign_number_id: int) -> str:
        session = self._read_session(campaign_number_id)
        if not session:
            return self._hangup_twiml()

        try:
            current_turn = dict(session.get("current_turn") or {})
            if current_turn:
                return self._twiml_for_turn(campaign_number_id, session, current_turn)

            opening_turn = self._build_opening_turn(campaign_number_id, session)
            if not opening_turn:
                return self._hangup_twiml()

            session["current_turn"] = opening_turn
            self._write_session(campaign_number_id, session)
            return self._twiml_for_turn(campaign_number_id, session, opening_turn)
        except Exception as exc:
            category, detail = classify_ai_runtime_error(str(exc))
            update_campaign_number_ai_observability(
                campaign_number_id,
                ai_runtime_error=f"[{category}] {detail}",
            )
            return self._hangup_twiml()

    def build_followup_twiml(self, campaign_number_id: int, *, user_input: str) -> str:
        session = self._read_session(campaign_number_id)
        if not session:
            return self._hangup_twiml()

        try:
            previous_turn = dict(session.get("current_turn") or {})
            previous_audio_token = str(previous_turn.get("audio_token") or "")
            if previous_audio_token:
                self._delete_audio_artifact(campaign_number_id, previous_audio_token)

            normalized_user_input = (user_input or "").strip()
            if normalized_user_input:
                session["no_input_turns"] = 0
                session.setdefault("history", []).append({"role": "user", "content": normalized_user_input})
                update_campaign_number_ai_observability(
                    campaign_number_id,
                    ai_last_user_input=normalized_user_input[:500],
                )
                next_turn = self._build_model_turn(campaign_number_id, session)
            else:
                session["no_input_turns"] = int(session.get("no_input_turns") or 0) + 1
                update_campaign_number_ai_observability(
                    campaign_number_id,
                    ai_no_input_turns=int(session.get("no_input_turns") or 0),
                )
                if int(session.get("no_input_turns") or 0) > MAX_NO_INPUT_TURNS:
                    self._write_session(campaign_number_id, session)
                    return self._hangup_twiml()
                next_turn = self._create_assistant_turn(
                    campaign_number_id,
                    session,
                    assistant_text="I did not catch that. Are you still there?",
                    should_transfer=False,
                )

            if not next_turn:
                self._write_session(campaign_number_id, session)
                return self._hangup_twiml()

            session["current_turn"] = next_turn
            self._write_session(campaign_number_id, session)
            return self._twiml_for_turn(campaign_number_id, session, next_turn)
        except Exception as exc:
            category, detail = classify_ai_runtime_error(str(exc))
            update_campaign_number_ai_observability(
                campaign_number_id,
                ai_runtime_error=f"[{category}] {detail}",
            )
            return self._hangup_twiml()

    def _build_opening_turn(self, campaign_number_id: int, session_payload: dict[str, Any]) -> dict[str, Any] | None:
        if int(session_payload.get("turn_count") or 0) >= MAX_AGENT_TURNS:
            return None

        agent = self._agent_from_session(session_payload)
        opening_history = list(session_payload.get("history") or []) + [
            {
                "role": "user",
                "content": "Start this phone call now with one short greeting and one concise qualifying question.",
            }
        ]
        openai_turn = self._request_openai_turn(agent, opening_history)
        return self._create_assistant_turn(
            campaign_number_id,
            session_payload,
            assistant_text=openai_turn.get("assistant_text") or "Hello, this is a quick follow-up call.",
            should_transfer=bool(openai_turn.get("should_transfer")),
            handoff_reason=openai_turn.get("handoff_reason"),
            persist_assistant_history=True,
        )

    def _build_model_turn(self, campaign_number_id: int, session_payload: dict[str, Any]) -> dict[str, Any] | None:
        if int(session_payload.get("turn_count") or 0) >= MAX_AGENT_TURNS:
            return self._create_assistant_turn(
                campaign_number_id,
                session_payload,
                assistant_text="Let me connect you with a specialist now.",
                should_transfer=True,
                handoff_reason="Reached max AI turn limit",
                persist_assistant_history=True,
            )

        agent = self._agent_from_session(session_payload)
        history = list(session_payload.get("history") or [])
        openai_turn = self._request_openai_turn(agent, history)
        return self._create_assistant_turn(
            campaign_number_id,
            session_payload,
            assistant_text=openai_turn.get("assistant_text") or "Hello, this is a quick follow-up call.",
            should_transfer=bool(openai_turn.get("should_transfer")),
            handoff_reason=openai_turn.get("handoff_reason"),
            persist_assistant_history=True,
        )

    def _agent_from_session(self, session_payload: dict[str, Any]) -> AIAgent:
        agent_data = dict(session_payload.get("agent") or {})
        return AIAgent(
            id=int(agent_data.get("id") or 0),
            user_id=int(session_payload.get("user_id") or self.user_id),
            name=str(agent_data.get("name") or "Agent"),
            system_prompt=str(agent_data.get("system_prompt") or ""),
            is_active=True,
            language=str(agent_data.get("language") or "en"),
            voice_id=str(agent_data.get("voice_id") or ""),
            model=str(agent_data.get("model") or settings.OPENAI_DEFAULT_MODEL),
            temperature=float(agent_data.get("temperature") or 0.7),
            handoff_description=str(agent_data.get("handoff_description") or ""),
        )

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
            raise ValueError("Campaign number not found for AI runtime")

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

        session_payload = {
            "campaign_number_id": int(campaign_number_id),
            "campaign_id": int(campaign.id),
            "user_id": int(campaign.user_id),
            "provider": self.provider,
            "from_number": str(from_number or "").strip(),
            "to_number": str(to_number or "").strip(),
            "transfer_number": str(transfer_number or "").strip(),
            "agent": {
                "id": int(agent.id),
                "name": str(agent.name or "Agent").strip(),
                "system_prompt": str(agent.system_prompt or "").strip(),
                "language": str(agent.language or "en").strip().lower(),
                "voice_id": str(agent.voice_id or "").strip(),
                "model": _chat_model_for_agent(agent.model),
                "temperature": float(agent.temperature or 0.7),
                "handoff_description": str(agent.handoff_description or "").strip(),
            },
            "history": [],
            "turn_count": 0,
            "no_input_turns": 0,
            "current_turn": None,
            "created_at_ms": int(time.time() * 1000),
            "updated_at_ms": int(time.time() * 1000),
        }
        self._write_session(campaign_number_id, session_payload)

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
            "model": _chat_model_for_agent(agent.model),
            "temperature": float(agent.temperature or 0.7),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{agent.system_prompt.strip()} "
                        f"Handoff guidance: {(agent.handoff_description or 'Transfer only when appropriate.').strip()}"
                    ).strip(),
                }
            ] + (history or [])[-MAX_HISTORY_MESSAGES:],
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

    def _request_elevenlabs_audio(self, *, voice_id: str, text: str) -> bytes:
        safe_voice_id = str(voice_id or "").strip()
        if not safe_voice_id:
            raise RuntimeError("ElevenLabs voice ID is empty")

        url = f"{settings.ELEVENLABS_API_BASE.rstrip('/')}/text-to-speech/{quote(safe_voice_id)}"
        output_format = str(settings.ELEVENLABS_OUTPUT_FORMAT or "").strip()
        if output_format:
            url = f"{url}?output_format={quote(output_format)}"

        headers = {
            "xi-api-key": self.elevenlabs_api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "text": self._sanitize_assistant_text(text),
        }
        if str(settings.ELEVENLABS_TTS_MODEL_ID or "").strip():
            payload["model_id"] = str(settings.ELEVENLABS_TTS_MODEL_ID).strip()

        max_attempts = max(1, int(settings.AI_HTTP_MAX_RETRIES) + 1)
        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=float(settings.OPENAI_REQUEST_TIMEOUT_SECONDS)) as client:
                    response = client.post(url, headers=headers, json=payload)

                if response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts:
                    backoff_seconds = float(settings.AI_HTTP_RETRY_BACKOFF_SECONDS) * attempt
                    time.sleep(backoff_seconds)
                    continue

                if response.status_code >= 400:
                    body_preview = (response.text or "").strip()[:300] or "no response body"
                    raise RuntimeError(
                        f"ElevenLabs TTS failed: status={response.status_code} body={body_preview}"
                    )

                self._validate_audio_response(response)
                return response.content
            except Exception as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(f"ElevenLabs TTS failed after {max_attempts} attempt(s): {exc}") from exc
                backoff_seconds = float(settings.AI_HTTP_RETRY_BACKOFF_SECONDS) * attempt
                time.sleep(backoff_seconds)

        raise RuntimeError("ElevenLabs TTS failed unexpectedly")

    def _request_elevenlabs_audio_with_fallback(self, voice_id: str, text: str) -> tuple[bytes, str]:
        primary_voice = str(voice_id or "").strip()
        fallback_voice = str(settings.ELEVENLABS_DEFAULT_VOICE_ID or "").strip()

        voice_candidates = [candidate for candidate in [primary_voice, fallback_voice] if candidate]
        if not voice_candidates:
            raise RuntimeError("ElevenLabs voice ID is not configured")

        last_error = ""
        for index, candidate_voice in enumerate(voice_candidates):
            try:
                audio_bytes = self._request_elevenlabs_audio(voice_id=candidate_voice, text=text)
                return audio_bytes, candidate_voice
            except Exception as exc:
                last_error = str(exc)
                if index == 0 and candidate_voice != fallback_voice:
                    lowered = last_error.lower()
                    if "status=404" in lowered or "voice" in lowered:
                        continue
                break

        raise RuntimeError(last_error or "ElevenLabs TTS failed")

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
        campaign_number_id: int,
        session_payload: dict[str, Any],
        *,
        assistant_text: str,
        should_transfer: bool,
        handoff_reason: str | None = None,
        persist_assistant_history: bool = False,
    ) -> dict[str, Any]:
        sanitized_text = self._sanitize_assistant_text(assistant_text)
        normalized_handoff_reason = _truncate_text(handoff_reason, 500)

        turn_count = int(session_payload.get("turn_count") or 0) + 1
        session_payload["turn_count"] = turn_count
        session_payload["updated_at_ms"] = int(time.time() * 1000)

        if persist_assistant_history and sanitized_text:
            session_payload.setdefault("history", []).append({"role": "assistant", "content": sanitized_text})
            if len(session_payload["history"]) > MAX_HISTORY_MESSAGES:
                session_payload["history"] = session_payload["history"][-MAX_HISTORY_MESSAGES:]

        update_kwargs: dict[str, Any] = {
            "ai_turn_count": turn_count,
            "ai_no_input_turns": int(session_payload.get("no_input_turns") or 0),
            "ai_last_assistant_text": sanitized_text[:500],
        }
        if normalized_handoff_reason:
            update_kwargs["ai_handoff_reason"] = normalized_handoff_reason
        update_campaign_number_ai_observability(campaign_number_id, **update_kwargs)

        if should_transfer:
            return {
                "assistant_text": sanitized_text,
                "should_transfer": True,
                "handoff_reason": normalized_handoff_reason,
                "audio_token": "",
                "voice_id": "",
            }

        audio_bytes, used_voice = self._request_elevenlabs_audio_with_fallback(
            str((session_payload.get("agent") or {}).get("voice_id") or "").strip(),
            sanitized_text,
        )
        audio_token = self._store_audio_artifact(campaign_number_id, audio_bytes)
        return {
            "assistant_text": sanitized_text,
            "should_transfer": False,
            "handoff_reason": normalized_handoff_reason,
            "audio_token": audio_token,
            "voice_id": used_voice,
        }

    def _twiml_for_turn(
        self,
        campaign_number_id: int,
        session_payload: dict[str, Any],
        current_turn: dict[str, Any],
    ) -> str:
        if current_turn.get("should_transfer"):
            return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Response>
    <Dial callerId=\"{session_payload['from_number']}\" timeout=\"30\">
        <Number>{session_payload['transfer_number']}</Number>
    </Dial>
</Response>"""

        audio_token = str(current_turn.get("audio_token") or "").strip()
        if not audio_token:
            return self._hangup_twiml()

        base_url = settings.BASE_URL.rstrip("/")
        audio_url = f"{base_url}/api/ai-runtime/audio/{campaign_number_id}/{audio_token}"
        gather_action_url = f"{base_url}/api/ai-runtime/twiml/{campaign_number_id}/gather"
        gather_timeout_seconds = max(1, int(settings.AI_GATHER_TIMEOUT_SECONDS))
        speech_timeout_seconds = max(1, int(settings.AI_GATHER_SPEECH_TIMEOUT_SECONDS))
        post_play_pause_seconds = max(0, int(settings.AI_GATHER_POST_PLAY_PAUSE_SECONDS))
        pause_block = (
            f"\n    <Pause length=\"{post_play_pause_seconds}\"/>"
            if post_play_pause_seconds
            else ""
        )

        return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Response>
    <Play>{audio_url}</Play>{pause_block}
    <Gather input=\"speech dtmf\" speechTimeout=\"{speech_timeout_seconds}\" timeout=\"{gather_timeout_seconds}\" action=\"{gather_action_url}\" method=\"POST\" actionOnEmptyResult=\"true\"/>
    <Hangup/>
</Response>"""

    def _hangup_twiml(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    def _session_path(self, campaign_number_id: int) -> Path:
        return AI_RUNTIME_DIR / f"{campaign_number_id}.json"

    def _audio_path(self, campaign_number_id: int, audio_token: str) -> Path:
        return AI_RUNTIME_DIR / f"{campaign_number_id}-{audio_token}.mp3"

    def _store_audio_artifact(self, campaign_number_id: int, audio_bytes: bytes) -> str:
        token = secrets.token_urlsafe(16).replace("-", "").replace("_", "")
        if not token:
            token = secrets.token_hex(12)
        self._audio_path(campaign_number_id, token).write_bytes(audio_bytes)
        return token

    def _delete_audio_artifact(self, campaign_number_id: int, audio_token: str) -> None:
        token = str(audio_token or "").strip()
        if not token:
            return
        self._audio_path(campaign_number_id, token).unlink(missing_ok=True)

    def _read_session(self, campaign_number_id: int) -> Optional[dict[str, Any]]:
        path = self._session_path(campaign_number_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _write_session(self, campaign_number_id: int, payload: dict[str, Any]) -> None:
        payload = dict(payload or {})
        payload["updated_at_ms"] = int(time.time() * 1000)
        self._session_path(campaign_number_id).write_text(json.dumps(payload))


def _build_service_from_runtime_session(campaign_number_id: int) -> tuple[AICallRuntimeService, Session] | None:
    session_path = AI_RUNTIME_DIR / f"{campaign_number_id}.json"
    if not session_path.exists():
        return None

    try:
        payload = json.loads(session_path.read_text())
    except Exception:
        return None

    user_id = int(payload.get("user_id") or 0)
    provider = str(payload.get("provider") or "signalwire").strip().lower()
    if not user_id:
        return None

    db = SessionLocal()
    try:
        service = AICallRuntimeService(db, user_id, provider=provider)
        return service, db
    except Exception:
        db.close()
        return None


def build_ai_runtime_twiml(campaign_number_id: int) -> str:
    service_bundle = _build_service_from_runtime_session(campaign_number_id)
    if not service_bundle:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    service, db = service_bundle
    try:
        return service.build_initial_twiml(campaign_number_id)
    finally:
        db.close()


def build_ai_runtime_followup_twiml(campaign_number_id: int, user_input: str) -> str:
    service_bundle = _build_service_from_runtime_session(campaign_number_id)
    if not service_bundle:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    service, db = service_bundle
    try:
        return service.build_followup_twiml(campaign_number_id, user_input=user_input)
    finally:
        db.close()


def build_ai_realtime_stream_twiml(campaign_number_id: int, token: str) -> str:
    ws_base = _ws_base_from_public_url(settings.BASE_URL.rstrip("/"))
    safe_token = str(token or "").strip()
    ws_url = f"{ws_base}/api/ai-realtime/ws/{campaign_number_id}/{safe_token}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""


def get_ai_runtime_audio(campaign_number_id: int, audio_token: str) -> Optional[bytes]:
    token = str(audio_token or "").strip()
    if not token or not token.isalnum():
        return None

    path = AI_RUNTIME_DIR / f"{campaign_number_id}-{token}.mp3"
    if not path.exists() or not path.is_file():
        return None

    try:
        return path.read_bytes()
    except Exception:
        return None


def cleanup_ai_runtime_artifacts(campaign_number_id: int) -> None:
    """Cleanup hook for worker finalization."""
    bridge_tuple = _bridge_threads.pop(campaign_number_id, None)
    if bridge_tuple:
        bridge, _thread = bridge_tuple
        try:
            bridge.stop()
        except Exception:
            pass

    session_path = AI_RUNTIME_DIR / f"{campaign_number_id}.json"
    if session_path.exists():
        session_path.unlink(missing_ok=True)

    for audio_path in AI_RUNTIME_DIR.glob(f"{campaign_number_id}-*.mp3"):
        audio_path.unlink(missing_ok=True)


def prune_stale_ai_runtime_artifacts(max_age_seconds: int | None = None) -> int:
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
