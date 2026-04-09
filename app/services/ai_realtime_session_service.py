"""
Shared AI Realtime session registry in Redis.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any

try:
    from redis import Redis
except Exception:  # pragma: no cover - optional when realtime runtime is inactive
    Redis = None  # type: ignore[assignment]

from app.config import get_settings

settings = get_settings()


def _session_key(campaign_number_id: int) -> str:
    return f"ai_rt:session:{int(campaign_number_id)}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


class AIRealtimeSessionService:
    def __init__(self, redis_url: str | None = None):
        if Redis is None:
            raise RuntimeError("redis package is required for AI realtime session service")
        self.redis_url = redis_url or settings.REDIS_URL
        self.redis = Redis.from_url(self.redis_url, decode_responses=True)

    def create_session(
        self,
        *,
        campaign_number_id: int,
        campaign_id: int,
        user_id: int,
        ai_agent_id: int,
        from_number: str,
        to_number: str,
        transfer_number: str,
    ) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        session = {
            "session_id": secrets.token_hex(12),
            "auth_token": secrets.token_urlsafe(24),
            "campaign_number_id": int(campaign_number_id),
            "campaign_id": int(campaign_id),
            "user_id": int(user_id),
            "ai_agent_id": int(ai_agent_id),
            "from_number": str(from_number or "").strip(),
            "to_number": str(to_number or "").strip(),
            "transfer_number": str(transfer_number or "").strip(),
            "call_sid": "",
            "stream_sid": "",
            "status": "created",
            "turn_count": 0,
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "first_audio_out_at_ms": 0,
            "handoff_reason": "",
            "error_category": "",
            "error_detail": "",
        }
        self.redis.setex(
            _session_key(campaign_number_id),
            int(settings.AI_REALTIME_SESSION_TTL_SECONDS),
            json.dumps(session),
        )
        return session

    def get_session(self, campaign_number_id: int) -> dict[str, Any] | None:
        raw = self.redis.get(_session_key(campaign_number_id))
        if not raw:
            return None
        try:
            return dict(json.loads(raw))
        except Exception:
            return None

    def update_session(self, campaign_number_id: int, **updates: Any) -> dict[str, Any] | None:
        current = self.get_session(campaign_number_id)
        if not current:
            return None
        current.update({k: v for k, v in updates.items() if v is not None})
        current["updated_at_ms"] = int(time.time() * 1000)
        self.redis.setex(
            _session_key(campaign_number_id),
            int(settings.AI_REALTIME_SESSION_TTL_SECONDS),
            json.dumps(current),
        )
        return current

    def validate_auth_token(self, campaign_number_id: int, token: str) -> bool:
        session = self.get_session(campaign_number_id)
        if not session:
            return False
        return str(session.get("auth_token") or "") == str(token or "")

    def mark_error(self, campaign_number_id: int, *, category: str, detail: str) -> dict[str, Any] | None:
        return self.update_session(
            campaign_number_id,
            status="error",
            error_category=(category or "").strip()[:60],
            error_detail=(detail or "").strip()[:500],
        )

    def set_call_sid(self, campaign_number_id: int, call_sid: str) -> dict[str, Any] | None:
        return self.update_session(campaign_number_id, call_sid=str(call_sid or "").strip())

    def set_stream_sid(self, campaign_number_id: int, stream_sid: str) -> dict[str, Any] | None:
        return self.update_session(campaign_number_id, stream_sid=str(stream_sid or "").strip())

    def increment_turn_count(self, campaign_number_id: int) -> dict[str, Any] | None:
        session = self.get_session(campaign_number_id)
        if not session:
            return None
        return self.update_session(
            campaign_number_id,
            turn_count=_safe_int(session.get("turn_count"), 0) + 1,
        )

    def end_session(self, campaign_number_id: int, status: str = "ended") -> dict[str, Any] | None:
        return self.update_session(campaign_number_id, status=status)
