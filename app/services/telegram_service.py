"""Telegram Bot notifications for completed AI calls."""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Iterable

import httpx

from app.models import CampaignNumber, UserTelegramConfig
from app.services.ai_call_runtime_service import AI_RUNTIME_DIR

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4000  # below Telegram's 4096 to leave room for HTML tags
HTTP_TIMEOUT_SECONDS = 15.0


def _parse_keywords(keywords_csv: str | None) -> list[str]:
    if not keywords_csv:
        return []
    return [kw.strip().lower() for kw in keywords_csv.split(",") if kw.strip()]


def matches_keywords(transcript_text: str, keywords_csv: str | None) -> bool:
    """True if any keyword appears in transcript. Empty list ⇒ always True."""
    keywords = _parse_keywords(keywords_csv)
    if not keywords:
        return True
    haystack = transcript_text.lower()
    return any(kw in haystack for kw in keywords)


def load_transcript(campaign_number_id: int) -> list[dict]:
    """Load conversation history from the runtime session JSON, if present."""
    session_path: Path = AI_RUNTIME_DIR / f"{campaign_number_id}.json"
    if not session_path.exists():
        return []
    try:
        payload = json.loads(session_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Telegram: failed to read session JSON for %s: %s", campaign_number_id, exc)
        return []
    history = payload.get("history") or []
    return [turn for turn in history if isinstance(turn, dict) and turn.get("content")]


def transcript_plain_text(transcript: Iterable[dict]) -> str:
    """Concatenate transcript turns into a single plain string for keyword matching."""
    return "\n".join(str(turn.get("content") or "") for turn in transcript)


def _format_metadata(number: CampaignNumber) -> str:
    status_value = number.status.value if hasattr(number.status, "value") else str(number.status)
    lines = [
        "<b>Chamada finalizada</b>",
        f"<b>Número:</b> {html.escape(number.phone_number or '-')}",
    ]
    if number.lead_name:
        lines.append(f"<b>Lead:</b> {html.escape(number.lead_name)}")
    lines.append(f"<b>Status:</b> {html.escape(status_value)}")
    lines.append(f"<b>Duração:</b> {int(number.duration_seconds or 0)}s")
    lines.append(f"<b>Custo:</b> ${float(number.cost or 0.0):.4f}")
    if number.answered_by:
        lines.append(f"<b>Atendido por:</b> {html.escape(number.answered_by)}")
    if number.ai_handoff_reason:
        lines.append(f"<b>Handoff:</b> {html.escape(number.ai_handoff_reason)}")
    return "\n".join(lines)


def _format_transcript(transcript: list[dict]) -> str:
    if not transcript:
        return "<i>(transcrição indisponível)</i>"
    rendered = []
    for turn in transcript:
        role = (turn.get("role") or "").lower()
        label = "🤖 AI" if role == "assistant" else "👤 Cliente" if role == "user" else role or "?"
        content = html.escape(str(turn.get("content") or "").strip())
        rendered.append(f"<b>{label}:</b> {content}")
    return "\n\n".join(rendered)


def build_messages(number: CampaignNumber, transcript: list[dict]) -> list[str]:
    """Build one or more <=4000 char Telegram HTML messages."""
    header = _format_metadata(number)
    body = _format_transcript(transcript)
    full = f"{header}\n\n<b>Transcrição:</b>\n{body}"

    if len(full) <= MAX_MESSAGE_LENGTH:
        return [full]

    chunks: list[str] = [header]
    current = "<b>Transcrição:</b>"
    for turn in transcript:
        role = (turn.get("role") or "").lower()
        label = "🤖 AI" if role == "assistant" else "👤 Cliente" if role == "user" else role or "?"
        content = html.escape(str(turn.get("content") or "").strip())
        piece = f"\n\n<b>{label}:</b> {content}"
        if len(current) + len(piece) > MAX_MESSAGE_LENGTH:
            chunks.append(current)
            current = piece.lstrip()
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks


async def _send_one(client: httpx.AsyncClient, bot_token: str, chat_id: str, text: str) -> None:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    response = await client.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    response.raise_for_status()


async def send_call_transcript(
    config: UserTelegramConfig,
    number: CampaignNumber,
    transcript: list[dict],
) -> bool:
    """Send formatted call transcript to the configured Telegram chat."""
    messages = build_messages(number, transcript)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            for msg in messages:
                await _send_one(client, config.bot_token, config.chat_id, msg)
    except httpx.HTTPError as exc:
        logger.warning("Telegram send failed for number %s: %s", number.id, exc)
        return False
    return True


async def send_test_message(bot_token: str, chat_id: str) -> tuple[bool, str]:
    """Send a one-off test ping. Returns (success, error_message)."""
    text = "✅ ColdCalls conectado ao Telegram."
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            await _send_one(client, bot_token, chat_id, text)
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("description", "")
        except Exception:
            detail = exc.response.text[:200]
        return False, f"HTTP {exc.response.status_code}: {detail}"
    except httpx.HTTPError as exc:
        return False, str(exc)
    return True, ""
