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


def _column_is_nullable(table_name: str, column_name: str) -> bool:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return False
    for col in inspector.get_columns(table_name):
        if col["name"] == column_name:
            return bool(col.get("nullable", False))
    return False


def _ensure_campaign_audio_nullable(conn):
    """
    SQLite cannot ALTER COLUMN nullability directly, so rebuild campaigns table.
    """
    if _column_is_nullable("campaigns", "audio_id"):
        return

    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text("DROP TABLE IF EXISTS campaigns_new"))
    conn.execute(
        text(
            """
            CREATE TABLE campaigns_new (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name VARCHAR(255) NOT NULL,
                caller_id_id INTEGER NOT NULL,
                country_id INTEGER NOT NULL,
                audio_id INTEGER NULL,
                ai_agent_id INTEGER,
                campaign_mode VARCHAR(20) NOT NULL DEFAULT 'audio',
                status VARCHAR(20),
                press_1_to_talk_with_agent BOOLEAN NOT NULL DEFAULT 0,
                voice_provider VARCHAR(20) NOT NULL DEFAULT 'twilio',
                max_concurrent_calls INTEGER NOT NULL DEFAULT 1,
                total_numbers INTEGER,
                processed_numbers INTEGER,
                successful_calls INTEGER,
                failed_calls INTEGER,
                total_cost FLOAT,
                created_at DATETIME,
                started_at DATETIME,
                completed_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(caller_id_id) REFERENCES caller_ids(id),
                FOREIGN KEY(country_id) REFERENCES countries(id),
                FOREIGN KEY(audio_id) REFERENCES audios(id)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO campaigns_new (
                id, user_id, name, caller_id_id, country_id, audio_id, ai_agent_id,
                campaign_mode, status,
                press_1_to_talk_with_agent, voice_provider, max_concurrent_calls,
                total_numbers, processed_numbers, successful_calls, failed_calls,
                total_cost, created_at, started_at, completed_at
            )
            SELECT
                id, user_id, name, caller_id_id, country_id, audio_id, ai_agent_id,
                campaign_mode, status,
                press_1_to_talk_with_agent, voice_provider, max_concurrent_calls,
                total_numbers, processed_numbers, successful_calls, failed_calls,
                total_cost, created_at, started_at, completed_at
            FROM campaigns
            """
        )
    )
    conn.execute(text("DROP TABLE campaigns"))
    conn.execute(text("ALTER TABLE campaigns_new RENAME TO campaigns"))

    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaigns_id ON campaigns(id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaigns_user_id ON campaigns(user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaigns_status ON campaigns(status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaigns_voice_provider ON campaigns(voice_provider)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaigns_campaign_mode ON campaigns(campaign_mode)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaigns_ai_agent_id ON campaigns(ai_agent_id)"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


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

        if _column_exists("campaigns", "campaign_mode") is False:
            conn.execute(
                text(
                    "ALTER TABLE campaigns "
                    "ADD COLUMN campaign_mode VARCHAR(20) NOT NULL DEFAULT 'audio'"
                )
            )

        if _column_exists("campaigns", "ai_agent_id") is False:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN ai_agent_id INTEGER"))

        if _column_exists("campaigns", "audio_id") and not _column_is_nullable("campaigns", "audio_id"):
            _ensure_campaign_audio_nullable(conn)

        if _column_exists("caller_ids", "vox_callerid_id") is False:
            conn.execute(text("ALTER TABLE caller_ids ADD COLUMN vox_callerid_id INTEGER"))

        if _column_exists("caller_ids", "vox_verification_status") is False:
            conn.execute(
                text(
                    "ALTER TABLE caller_ids "
                    "ADD COLUMN vox_verification_status VARCHAR(20) NOT NULL DEFAULT 'not_started'"
                )
            )

        if _column_exists("caller_ids", "vox_last_verification_at") is False:
            conn.execute(text("ALTER TABLE caller_ids ADD COLUMN vox_last_verification_at DATETIME"))

        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaigns_campaign_mode ON campaigns(campaign_mode)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaigns_ai_agent_id ON campaigns(ai_agent_id)"))

        if _column_exists("campaign_numbers", "ai_turn_count") is False:
            conn.execute(text("ALTER TABLE campaign_numbers ADD COLUMN ai_turn_count INTEGER"))
        if _column_exists("campaign_numbers", "ai_no_input_turns") is False:
            conn.execute(text("ALTER TABLE campaign_numbers ADD COLUMN ai_no_input_turns INTEGER"))
        if _column_exists("campaign_numbers", "ai_last_user_input") is False:
            conn.execute(text("ALTER TABLE campaign_numbers ADD COLUMN ai_last_user_input TEXT"))
        if _column_exists("campaign_numbers", "ai_last_assistant_text") is False:
            conn.execute(text("ALTER TABLE campaign_numbers ADD COLUMN ai_last_assistant_text TEXT"))
        if _column_exists("campaign_numbers", "ai_handoff_reason") is False:
            conn.execute(text("ALTER TABLE campaign_numbers ADD COLUMN ai_handoff_reason TEXT"))
        if _column_exists("campaign_numbers", "ai_runtime_error") is False:
            conn.execute(text("ALTER TABLE campaign_numbers ADD COLUMN ai_runtime_error TEXT"))
        if _column_exists("campaign_numbers", "lead_name") is False:
            conn.execute(text("ALTER TABLE campaign_numbers ADD COLUMN lead_name VARCHAR(255)"))
