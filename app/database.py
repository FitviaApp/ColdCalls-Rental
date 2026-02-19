"""
Database configuration with SQLAlchemy
"""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import get_settings

settings = get_settings()

# Create engine
engine = create_engine(
    settings.DATABASE_URL,
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
