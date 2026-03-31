"""
Sync helpers for ElevenLabs agent runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AIAgent, AIAgentRuntimeProvider
from app.services.user_elevenlabs_service import get_user_elevenlabs_credentials

settings = get_settings()


class ElevenLabsAgentSyncError(RuntimeError):
    """Raised when ElevenLabs agent sync fails."""


def _truncate_text(value: str | None, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _extract_agent_id(payload: dict[str, Any]) -> str | None:
    direct_keys = (
        "agent_id",
        "id",
    )
    for key in direct_keys:
        value = payload.get(key)
        if value:
            return str(value).strip()

    nested_agent = payload.get("agent")
    if isinstance(nested_agent, dict):
        for key in direct_keys:
            value = nested_agent.get(key)
            if value:
                return str(value).strip()
    return None


class ElevenLabsAgentSyncService:
    """Thin HTTP client for ElevenLabs agent APIs."""

    def __init__(self, api_key: str):
        api_key = (api_key or "").strip()
        if not api_key:
            raise ElevenLabsAgentSyncError("ElevenLabs API key is missing")
        self.api_key = api_key

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{settings.ELEVENLABS_SYNC_API_BASE.rstrip('/')}{path}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout_seconds = float(settings.ELEVENLABS_SYNC_TIMEOUT_SECONDS or 20.0)
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.request(
                method=method,
                url=url,
                headers=headers,
                json=json_payload,
                params=params,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body_preview = _truncate_text(response.text, 220)
            raise ElevenLabsAgentSyncError(
                f"ElevenLabs API request failed ({response.status_code}): {body_preview}"
            ) from exc
        if not response.content:
            return {}
        try:
            return response.json()
        except Exception as exc:
            raise ElevenLabsAgentSyncError("ElevenLabs API returned invalid JSON") from exc

    def get_user(self) -> dict[str, Any]:
        return self._request("GET", "/user")

    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/convai/agents", json_payload=payload)

    def update_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/convai/agents/{agent_id}", json_payload=payload)

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/convai/agents/{agent_id}")

    def build_agent_payload(
        self,
        *,
        app_agent: AIAgent,
        transfer_number: str,
    ) -> dict[str, Any]:
        handoff_description = (
            app_agent.handoff_description
            or "Transfer to a human when the lead asks for a person or qualifies."
        ).strip()
        return {
            "name": app_agent.name.strip(),
            "conversation_config": {
                "agent": {
                    "prompt": {
                        "prompt": app_agent.system_prompt.strip(),
                    },
                    "language": (app_agent.language or "en").strip().lower(),
                }
            },
            "voice_id": app_agent.voice_id.strip(),
            "llm": {
                "model": (app_agent.model or settings.OPENAI_DEFAULT_MODEL).strip(),
                "temperature": float(app_agent.temperature or 0.7),
            },
            "tool_config": {
                "transfer_to_number": {
                    "enabled": True,
                    "to_number": transfer_number.strip(),
                    "description": handoff_description,
                }
            },
        }


def sync_ai_agent_to_elevenlabs(
    db: Session,
    user_id: int,
    agent: AIAgent,
    transfer_number: str,
) -> bool:
    """Sync a local AI agent to ElevenLabs. Returns True when sync succeeds."""
    if agent.runtime_provider != AIAgentRuntimeProvider.ELEVENLABS_AGENT:
        agent.last_sync_status = "Legacy runtime selected; sync not required."
        return True

    api_key = get_user_elevenlabs_credentials(db, user_id)
    if not api_key:
        agent.last_sync_status = "Sync failed: configure ElevenLabs API key in Settings first."
        return False
    if not transfer_number:
        agent.last_sync_status = "Sync failed: configure Transfer Number in Settings first."
        return False

    service = ElevenLabsAgentSyncService(api_key=api_key)
    payload = service.build_agent_payload(app_agent=agent, transfer_number=transfer_number)

    try:
        if agent.external_agent_id:
            response = service.update_agent(agent.external_agent_id, payload)
        else:
            response = service.create_agent(payload)

        external_agent_id = (
            _extract_agent_id(response)
            or (agent.external_agent_id.strip() if agent.external_agent_id else "")
        )
        if not external_agent_id:
            agent.last_sync_status = "Sync failed: ElevenLabs response did not include an agent ID."
            return False

        agent.external_agent_id = external_agent_id
        synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        agent.last_sync_status = f"Synced successfully at {synced_at} UTC."
        return True
    except Exception as exc:
        agent.last_sync_status = f"Sync failed: {_truncate_text(str(exc), 240)}"
        return False


def check_elevenlabs_credentials(api_key: str) -> tuple[bool, str]:
    """Validate ElevenLabs credentials with a lightweight API call."""
    try:
        ElevenLabsAgentSyncService(api_key=api_key).get_user()
        return True, "ElevenLabs API key is valid."
    except Exception as exc:
        return False, _truncate_text(str(exc), 240)
