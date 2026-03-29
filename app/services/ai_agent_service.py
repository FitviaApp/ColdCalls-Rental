"""
Helpers for user AI agent CRUD and validation.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AIAgent

settings = get_settings()

DEFAULT_AI_AGENT_MODEL = settings.OPENAI_DEFAULT_MODEL
DEFAULT_AI_AGENT_LANGUAGE = "en"
DEFAULT_AI_AGENT_TEMPERATURE = 0.7


def list_user_ai_agents(db: Session, user_id: int) -> list[AIAgent]:
    return db.query(AIAgent).filter(AIAgent.user_id == user_id).order_by(
        AIAgent.is_active.desc(),
        AIAgent.name.asc(),
    ).all()


def get_user_ai_agent(db: Session, user_id: int, agent_id: int) -> AIAgent | None:
    return db.query(AIAgent).filter(
        AIAgent.user_id == user_id,
        AIAgent.id == agent_id,
    ).first()


def create_user_ai_agent(
    db: Session,
    user_id: int,
    name: str,
    system_prompt: str,
    voice_id: str,
    model: str = DEFAULT_AI_AGENT_MODEL,
    temperature: float = DEFAULT_AI_AGENT_TEMPERATURE,
    language: str = DEFAULT_AI_AGENT_LANGUAGE,
    handoff_description: str = "",
    is_active: bool = True,
) -> AIAgent:
    agent = AIAgent(
        user_id=user_id,
        name=name.strip(),
        system_prompt=system_prompt.strip(),
        voice_id=voice_id.strip(),
        model=(model or DEFAULT_AI_AGENT_MODEL).strip(),
        temperature=float(temperature),
        language=(language or DEFAULT_AI_AGENT_LANGUAGE).strip().lower(),
        handoff_description=handoff_description.strip() or None,
        is_active=bool(is_active),
    )
    db.add(agent)
    db.flush()
    return agent


def update_user_ai_agent(
    agent: AIAgent,
    *,
    name: str,
    system_prompt: str,
    voice_id: str,
    model: str,
    temperature: float,
    language: str,
    handoff_description: str,
    is_active: bool,
) -> AIAgent:
    agent.name = name.strip()
    agent.system_prompt = system_prompt.strip()
    agent.voice_id = voice_id.strip()
    agent.model = (model or DEFAULT_AI_AGENT_MODEL).strip()
    agent.temperature = float(temperature)
    agent.language = (language or DEFAULT_AI_AGENT_LANGUAGE).strip().lower()
    agent.handoff_description = handoff_description.strip() or None
    agent.is_active = bool(is_active)
    return agent
