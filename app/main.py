"""
FastAPI Application Entry Point
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import init_db

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    # Startup
    init_db()
    create_admin_user()
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
from app.routers import auth, dashboard, campaigns, payments, admin, api  # noqa: E402

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(campaigns.router)
app.include_router(payments.router)
app.include_router(admin.router)
app.include_router(api.router)


@app.get("/")
async def root():
    """Redirect to dashboard or login"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/auth/login")


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy"}
