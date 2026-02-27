"""
Database configuration with SQLAlchemy
"""
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import get_settings

settings = get_settings()


def _normalize_database_url(url: str) -> str:
    """
    Normalize SQLite relative paths to absolute project-root paths.

    Prevents app/worker from using different DB files when started from
    different working directories.
    """
    if not url.startswith("sqlite:///"):
        return url

    # Keep absolute SQLite URLs unchanged (sqlite:////abs/path.db).
    if url.startswith("sqlite:////"):
        return url

    raw_path = url[len("sqlite:///"):]
    if raw_path.startswith("/"):
        return url

    project_root = Path(__file__).resolve().parent.parent
    absolute_path = (project_root / raw_path).resolve()
    return f"sqlite:///{absolute_path}"


DATABASE_URL = _normalize_database_url(settings.DATABASE_URL)

# Create engine
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}  # Needed for SQLite
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db():
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database tables"""
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _apply_schema_patches()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _apply_schema_patches():
    """
    Lightweight schema patches for existing SQLite databases without Alembic.
    """
    with engine.begin() as conn:
        if _column_exists("caller_ids", "user_id") is False:
            conn.execute(text("ALTER TABLE caller_ids ADD COLUMN user_id INTEGER"))

        if _column_exists("audios", "user_id") is False:
            conn.execute(text("ALTER TABLE audios ADD COLUMN user_id INTEGER"))

        if _column_exists("campaigns", "press_1_to_talk_with_agent") is False:
            conn.execute(
                text(
                    "ALTER TABLE campaigns "
                    "ADD COLUMN press_1_to_talk_with_agent BOOLEAN NOT NULL DEFAULT 0"
                )
            )

        if _column_exists("campaigns", "voice_provider") is False:
            conn.execute(
                text(
                    "ALTER TABLE campaigns "
                    "ADD COLUMN voice_provider VARCHAR(20) NOT NULL DEFAULT 'twilio'"
                )
            )

        if _column_exists("campaigns", "max_concurrent_calls") is False:
            conn.execute(
                text(
                    "ALTER TABLE campaigns "
                    "ADD COLUMN max_concurrent_calls INTEGER NOT NULL DEFAULT 1"
                )
            )
