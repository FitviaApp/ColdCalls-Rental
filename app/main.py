"""
FastAPI Application Entry Point
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from app.templating import Jinja2Templates

from app.config import get_settings
from app.database import init_db
from app.services.ai_campaign_readiness_service import get_ai_schema_health
from app.services.ai_realtime_session_service import get_redis_health
from app.services.worker_health_service import get_worker_health

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    # Startup
    init_db()
    create_admin_user()
    ensure_default_data()
    yield
    # Shutdown
    pass


def create_admin_user():
    """Create admin user if it doesn't exist"""
    from app.database import SessionLocal
    from app.models import User
    from app.auth import hash_password

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == settings.ADMIN_EMAIL).first()
        if not admin:
            admin = User(
                email=settings.ADMIN_EMAIL,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
                is_admin=True,
                is_active=True
            )
            db.add(admin)
            db.commit()
            print(f"Admin user created: {settings.ADMIN_EMAIL}")
    finally:
        db.close()


def ensure_default_data():
    """Ensure default plans and baseline data exist."""
    from app.database import SessionLocal
    from app.services.rental_service import ensure_default_rental_plans

    db = SessionLocal()
    try:
        ensure_default_rental_plans(db)
    finally:
        db.close()


app = FastAPI(
    title=settings.APP_NAME,
    description="Cold Calls Platform - Multi-user campaign management",
    version="1.0.0",
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Templates
templates = Jinja2Templates(directory="app/templates")


# Include routers
from app.routers import auth, dashboard, campaigns, admin, api, assets, billing, ai_agents  # noqa: E402

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(campaigns.router)
app.include_router(ai_agents.router)
app.include_router(billing.router)
app.include_router(assets.router)
app.include_router(admin.router)
app.include_router(api.router)


@app.get("/")
async def root():
    """Redirect to dashboard or login"""
    return RedirectResponse(url="/auth/login")


@app.get("/health")
async def health():
    """Health check endpoint"""
    schema_health = get_ai_schema_health()
    worker_health = get_worker_health()
    redis_health = get_redis_health()
    overall_status = "healthy"
    if not schema_health["ready"] or not worker_health["online"] or not redis_health["available"]:
        overall_status = "degraded"
    return {
        "status": overall_status,
        "schema": schema_health,
        "worker": worker_health,
        "redis": redis_health,
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Redirect browser traffic to billing when rental is required."""
    if exc.status_code == 402 and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/billing", status_code=302)

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers
    )
