"""
Worker heartbeat helpers shared across routers and worker runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


WORKER_HEARTBEAT_FILE = Path("/tmp/coldcalls_worker_heartbeat")


def get_worker_health(max_age_seconds: int = 60) -> dict[str, object]:
    try:
        if not WORKER_HEARTBEAT_FILE.exists():
            return {
                "online": False,
                "age_seconds": None,
                "last_heartbeat_at": None,
            }

        mtime = WORKER_HEARTBEAT_FILE.stat().st_mtime
        age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - float(mtime))
        return {
            "online": age_seconds <= float(max_age_seconds),
            "age_seconds": round(age_seconds, 1),
            "last_heartbeat_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        }
    except Exception:
        return {
            "online": False,
            "age_seconds": None,
            "last_heartbeat_at": None,
        }


def is_worker_online(max_age_seconds: int = 60) -> bool:
    return bool(get_worker_health(max_age_seconds=max_age_seconds).get("online"))


def touch_worker_heartbeat() -> None:
    WORKER_HEARTBEAT_FILE.touch()
