"""
Redis-backed event bus for AI Realtime sessions.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

AI_REALTIME_EVENT_VERSION = "v2"


def realtime_channel(campaign_number_id: int) -> str:
    return f"ai_rt:{int(campaign_number_id)}"


@dataclass
class AIRealtimeEvent:
    event: str
    campaign_number_id: int
    payload: dict[str, Any]
    version: str = AI_REALTIME_EVENT_VERSION
    timestamp_ms: int = 0

    def to_json(self) -> str:
        data = {
            "v": self.version,
            "event": self.event,
            "campaign_number_id": int(self.campaign_number_id),
            "timestamp_ms": int(self.timestamp_ms or int(time.time() * 1000)),
            "payload": self.payload or {},
        }
        return json.dumps(data)

    @classmethod
    def from_json(cls, raw: str) -> "AIRealtimeEvent":
        data = json.loads(raw or "{}")
        return cls(
            event=str(data.get("event") or ""),
            campaign_number_id=int(data.get("campaign_number_id") or 0),
            payload=dict(data.get("payload") or {}),
            version=str(data.get("v") or AI_REALTIME_EVENT_VERSION),
            timestamp_ms=int(data.get("timestamp_ms") or 0),
        )


class AIRealtimeEventBus:
    """Synchronous publisher/subscriber for worker threads."""

    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or settings.REDIS_URL
        self.redis = Redis.from_url(self.redis_url, decode_responses=True)

    def publish(self, campaign_number_id: int, event: str, payload: dict[str, Any] | None = None) -> None:
        message = AIRealtimeEvent(
            event=event,
            campaign_number_id=campaign_number_id,
            payload=payload or {},
        ).to_json()
        self.redis.publish(realtime_channel(campaign_number_id), message)

    def consume(
        self,
        campaign_number_id: int,
        *,
        stop_when: Callable[[], bool],
        on_event: Callable[[AIRealtimeEvent], None],
        poll_interval_seconds: float = 0.1,
    ) -> None:
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        channel = realtime_channel(campaign_number_id)
        pubsub.subscribe(channel)
        try:
            while not stop_when():
                message = pubsub.get_message(timeout=1.0)
                if not message:
                    time.sleep(poll_interval_seconds)
                    continue
                raw = str(message.get("data") or "")
                if not raw:
                    continue
                try:
                    on_event(AIRealtimeEvent.from_json(raw))
                except Exception as exc:
                    logger.warning("AI realtime consume handler failed: %s", exc)
        finally:
            try:
                pubsub.close()
            except Exception:
                pass


class AsyncAIRealtimeEventBus:
    """Async publisher/subscriber for API websocket handlers."""

    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or settings.REDIS_URL
        self.redis = AsyncRedis.from_url(self.redis_url, decode_responses=True)

    async def publish(self, campaign_number_id: int, event: str, payload: dict[str, Any] | None = None) -> None:
        message = AIRealtimeEvent(
            event=event,
            campaign_number_id=campaign_number_id,
            payload=payload or {},
        ).to_json()
        await self.redis.publish(realtime_channel(campaign_number_id), message)

    async def close(self) -> None:
        try:
            await self.redis.aclose()
        except Exception:
            pass

