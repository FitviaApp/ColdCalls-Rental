import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import app.database as database_module


class RuntimeHardeningTests(unittest.TestCase):
    def test_init_db_applies_ai_schema_patches_to_legacy_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE users (
                        id INTEGER NOT NULL PRIMARY KEY,
                        email VARCHAR(255) NOT NULL,
                        password_hash VARCHAR(255) NOT NULL
                    );
                    CREATE TABLE caller_ids (
                        id INTEGER NOT NULL PRIMARY KEY,
                        phone_number VARCHAR(20) NOT NULL,
                        country_code VARCHAR(5) NOT NULL,
                        description VARCHAR(255),
                        is_active BOOLEAN
                    );
                    CREATE TABLE audios (
                        id INTEGER NOT NULL PRIMARY KEY,
                        name VARCHAR(255) NOT NULL,
                        r2_key VARCHAR(500) NOT NULL,
                        r2_url VARCHAR(500) NOT NULL,
                        duration_seconds INTEGER,
                        is_active BOOLEAN
                    );
                    CREATE TABLE countries (
                        id INTEGER NOT NULL PRIMARY KEY,
                        code VARCHAR(5) NOT NULL,
                        name VARCHAR(100) NOT NULL,
                        price_per_minute FLOAT NOT NULL,
                        is_active BOOLEAN
                    );
                    CREATE TABLE campaigns (
                        id INTEGER NOT NULL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        name VARCHAR(255) NOT NULL,
                        caller_id_id INTEGER NOT NULL,
                        country_id INTEGER NOT NULL,
                        audio_id INTEGER,
                        status VARCHAR(20),
                        total_numbers INTEGER,
                        processed_numbers INTEGER,
                        successful_calls INTEGER,
                        failed_calls INTEGER,
                        total_cost FLOAT,
                        created_at DATETIME,
                        started_at DATETIME,
                        completed_at DATETIME
                    );
                    CREATE TABLE campaign_numbers (
                        id INTEGER NOT NULL PRIMARY KEY,
                        campaign_id INTEGER NOT NULL,
                        phone_number VARCHAR(20) NOT NULL,
                        status VARCHAR(20),
                        call_sid VARCHAR(50),
                        duration_seconds INTEGER,
                        cost FLOAT,
                        answered_by VARCHAR(50),
                        processed_at DATETIME,
                        error_message TEXT
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            temp_engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False},
            )
            original_engine = database_module.engine
            original_session_local = database_module.SessionLocal
            database_module.engine = temp_engine
            database_module.SessionLocal = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=temp_engine,
            )
            try:
                database_module.init_db()
                inspector = inspect(temp_engine)
                campaign_columns = {
                    column["name"] for column in inspector.get_columns("campaigns")
                }
                campaign_number_columns = {
                    column["name"] for column in inspector.get_columns("campaign_numbers")
                }
                table_names = set(inspector.get_table_names())
            finally:
                database_module.engine = original_engine
                database_module.SessionLocal = original_session_local
                temp_engine.dispose()

        self.assertIn("campaign_mode", campaign_columns)
        self.assertIn("ai_agent_id", campaign_columns)
        self.assertIn("ai_turn_count", campaign_number_columns)
        self.assertIn("ai_runtime_error", campaign_number_columns)
        self.assertIn("ai_agents", table_names)
        self.assertIn("user_openai_credentials", table_names)
        self.assertIn("user_elevenlabs_credentials", table_names)
        self.assertIn("user_signalwire_credentials", table_names)

    def test_start_app_wrapper_prefers_dot_venv(self):
        repo_root = Path(__file__).resolve().parent.parent
        wrapper = repo_root / "deploy/bin/start-app.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            for directory in [project_dir / ".venv/bin", project_dir / "venv/bin"]:
                directory.mkdir(parents=True, exist_ok=True)
            for binary in [project_dir / ".venv/bin/python", project_dir / ".venv/bin/uvicorn", project_dir / "venv/bin/python"]:
                binary.write_text("#!/usr/bin/env bash\nexit 0\n")
                binary.chmod(0o755)

            result = subprocess.run(
                ["bash", str(wrapper)],
                check=True,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "COLDCALLS_PROJECT_DIR": str(project_dir),
                    "COLDCALLS_STARTUP_DRY_RUN": "1",
                    "APP_HOST": "127.0.0.1",
                    "APP_PORT": "8000",
                },
            )

        self.assertIn(str(project_dir / ".venv/bin/uvicorn"), result.stdout.strip())

    def test_start_worker_wrapper_falls_back_to_venv(self):
        repo_root = Path(__file__).resolve().parent.parent
        wrapper = repo_root / "deploy/bin/start-worker.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "venv/bin").mkdir(parents=True, exist_ok=True)
            python_bin = project_dir / "venv/bin/python"
            python_bin.write_text("#!/usr/bin/env bash\nexit 0\n")
            python_bin.chmod(0o755)

            result = subprocess.run(
                ["bash", str(wrapper)],
                check=True,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "COLDCALLS_PROJECT_DIR": str(project_dir),
                    "COLDCALLS_STARTUP_DRY_RUN": "1",
                },
            )

        self.assertEqual(result.stdout.strip(), f"{python_bin} worker.py")
