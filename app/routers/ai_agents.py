"""
AI Agents Router - user-managed reusable AI voice agents.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_active_rental
from app.models import AIAgent, Campaign, CampaignMode, CampaignStatus, User
from app.services.ai_agent_service import (
    DEFAULT_AI_AGENT_LANGUAGE,
    DEFAULT_AI_AGENT_MODEL,
    DEFAULT_AI_AGENT_TEMPERATURE,
    create_user_ai_agent,
    get_user_ai_agent,
    list_user_ai_agents,
    update_user_ai_agent,
)

router = APIRouter(prefix="/ai-agents", tags=["ai_agents"])
templates = Jinja2Templates(directory="app/templates")


def _render_form(
    request: Request,
    user: User,
    *,
    agent: AIAgent | None = None,
    error: str | None = None,
    form_data: dict | None = None,
) -> dict:
    return {
        "request": request,
        "user": user,
        "agent": agent,
        "error": error,
        "form_data": form_data or {},
        "default_language": DEFAULT_AI_AGENT_LANGUAGE,
        "default_model": DEFAULT_AI_AGENT_MODEL,
        "default_temperature": DEFAULT_AI_AGENT_TEMPERATURE,
    }


def _agent_form_data(
    *,
    name: str = "",
    system_prompt: str = "",
    voice_id: str = "",
    model: str = DEFAULT_AI_AGENT_MODEL,
    temperature: float = DEFAULT_AI_AGENT_TEMPERATURE,
    language: str = DEFAULT_AI_AGENT_LANGUAGE,
    handoff_description: str = "",
    is_active: bool = True,
) -> dict:
    return {
        "name": name,
        "system_prompt": system_prompt,
        "voice_id": voice_id,
        "model": model,
        "temperature": temperature,
        "language": language,
        "handoff_description": handoff_description,
        "is_active": bool(is_active),
    }


@router.get("", response_class=HTMLResponse)
async def list_agents(
    request: Request,
    created: bool = False,
    updated: bool = False,
    toggled: bool = False,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db),
):
    agents = list_user_ai_agents(db, user.id)
    return templates.TemplateResponse(
        "ai_agents/list.html",
        {
            "request": request,
            "user": user,
            "agents": agents,
            "created": created,
            "updated": updated,
            "toggled": toggled,
        },
    )


@router.get("/create", response_class=HTMLResponse)
async def create_agent_page(
    request: Request,
    user: User = Depends(require_active_rental),
):
    return templates.TemplateResponse("ai_agents/create.html", _render_form(request, user))


@router.post("/create")
async def create_agent(
    request: Request,
    name: str = Form(...),
    system_prompt: str = Form(...),
    voice_id: str = Form(...),
    model: str = Form(default=DEFAULT_AI_AGENT_MODEL),
    temperature: float = Form(default=DEFAULT_AI_AGENT_TEMPERATURE),
    language: str = Form(default=DEFAULT_AI_AGENT_LANGUAGE),
    handoff_description: str = Form(default=""),
    is_active: bool = Form(default=True),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db),
):
    name = name.strip()
    system_prompt = system_prompt.strip()
    voice_id = voice_id.strip()
    model = model.strip() or DEFAULT_AI_AGENT_MODEL
    language = (language or DEFAULT_AI_AGENT_LANGUAGE).strip().lower()
    form_data = _agent_form_data(
        name=name,
        system_prompt=system_prompt,
        voice_id=voice_id,
        model=model,
        temperature=temperature,
        language=language,
        handoff_description=handoff_description,
        is_active=is_active,
    )

    if not name:
        return templates.TemplateResponse(
            "ai_agents/create.html",
            _render_form(request, user, error="Agent name cannot be empty.", form_data=form_data),
            status_code=400,
        )
    if not system_prompt:
        return templates.TemplateResponse(
            "ai_agents/create.html",
            _render_form(request, user, error="System prompt cannot be empty.", form_data=form_data),
            status_code=400,
        )
    if not voice_id:
        return templates.TemplateResponse(
            "ai_agents/create.html",
            _render_form(request, user, error="Voice ID cannot be empty.", form_data=form_data),
            status_code=400,
        )
    if not 0.0 <= float(temperature) <= 2.0:
        return templates.TemplateResponse(
            "ai_agents/create.html",
            _render_form(request, user, error="Temperature must be between 0.0 and 2.0.", form_data=form_data),
            status_code=400,
        )

    try:
        create_user_ai_agent(
            db,
            user.id,
            name=name,
            system_prompt=system_prompt,
            voice_id=voice_id,
            model=model,
            temperature=temperature,
            language=language,
            handoff_description=handoff_description,
            is_active=is_active,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "ai_agents/create.html",
            _render_form(request, user, error=str(exc), form_data=form_data),
            status_code=400,
        )
    db.commit()
    return RedirectResponse(url="/ai-agents?created=true", status_code=302)


@router.get("/{agent_id}/edit", response_class=HTMLResponse)
async def edit_agent_page(
    request: Request,
    agent_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db),
):
    agent = get_user_ai_agent(db, user.id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="AI agent not found")
    return templates.TemplateResponse(
        "ai_agents/edit.html",
        _render_form(
            request,
            user,
            agent=agent,
            form_data=_agent_form_data(
                name=agent.name,
                system_prompt=agent.system_prompt,
                voice_id=agent.voice_id,
                model=agent.model,
                temperature=agent.temperature,
                language=agent.language,
                handoff_description=agent.handoff_description or "",
                is_active=agent.is_active,
            ),
        ),
    )


@router.post("/{agent_id}/edit")
async def edit_agent(
    request: Request,
    agent_id: int,
    name: str = Form(...),
    system_prompt: str = Form(...),
    voice_id: str = Form(...),
    model: str = Form(default=DEFAULT_AI_AGENT_MODEL),
    temperature: float = Form(default=DEFAULT_AI_AGENT_TEMPERATURE),
    language: str = Form(default=DEFAULT_AI_AGENT_LANGUAGE),
    handoff_description: str = Form(default=""),
    is_active: bool = Form(default=False),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db),
):
    agent = get_user_ai_agent(db, user.id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="AI agent not found")
    form_data = _agent_form_data(
        name=name,
        system_prompt=system_prompt,
        voice_id=voice_id,
        model=model,
        temperature=temperature,
        language=language,
        handoff_description=handoff_description,
        is_active=is_active,
    )

    try:
        update_user_ai_agent(
            agent,
            name=name,
            system_prompt=system_prompt,
            voice_id=voice_id,
            model=model,
            temperature=temperature,
            language=language,
            handoff_description=handoff_description,
            is_active=is_active,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "ai_agents/edit.html",
            _render_form(request, user, agent=agent, error=str(exc), form_data=form_data),
            status_code=400,
        )

    db.commit()
    return RedirectResponse(url="/ai-agents?updated=true", status_code=302)


@router.post("/{agent_id}/toggle")
async def toggle_agent(
    agent_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db),
):
    agent = get_user_ai_agent(db, user.id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="AI agent not found")

    active_ai_campaign = db.query(Campaign).filter(
        Campaign.ai_agent_id == agent.id,
        Campaign.campaign_mode == CampaignMode.AI_AGENT,
    ).filter(Campaign.status == CampaignStatus.RUNNING).first()

    if active_ai_campaign and agent.is_active:
        raise HTTPException(status_code=400, detail="Pause the running campaign before disabling this agent")

    agent.is_active = not agent.is_active
    db.commit()
    return RedirectResponse(url="/ai-agents?toggled=true", status_code=302)
