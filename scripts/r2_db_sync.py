#!/usr/bin/env python3
"""Backup/restore coldcalls.db to/from R2 (ephemeral container disk)."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from app.config import get_settings  # noqa: E402

DB_PATH = PROJECT_ROOT / "coldcalls.db"
DB_BACKUP_KEY = "backups/coldcalls.db"


def _client():
    settings = get_settings()
    if not settings.R2_ACCOUNT_ID or not settings.R2_ACCESS_KEY_ID:
        return None, None
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )
    return client, settings.R2_BUCKET_NAME


def restore() -> None:
    client, bucket = _client()
    if not client or not bucket:
        print("R2 not configured; skipping DB restore")
        return

    try:
        client.head_object(Bucket=bucket, Key=DB_BACKUP_KEY)
    except Exception:
        print(f"No DB backup found at r2://{bucket}/{DB_BACKUP_KEY}")
        return

    if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        print("Local coldcalls.db already present; skipping restore")
        return

    tmp_path = DB_PATH.with_suffix(".db.restoring")
    client.download_file(bucket, DB_BACKUP_KEY, str(tmp_path))
    tmp_path.replace(DB_PATH)
    print(f"Restored {DB_PATH} from r2://{bucket}/{DB_BACKUP_KEY}")


def backup() -> None:
    client, bucket = _client()
    if not client or not bucket:
        return
    if not DB_PATH.exists():
        print("No local coldcalls.db to back up")
        return

    client.upload_file(
        str(DB_PATH),
        bucket,
        DB_BACKUP_KEY,
        ExtraArgs={"ContentType": "application/x-sqlite3"},
    )
    print(f"Backed up {DB_PATH} to r2://{bucket}/{DB_BACKUP_KEY}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "backup"
    if command == "restore":
        restore()
    elif command == "backup":
        backup()
    else:
        print(f"Unknown command: {command} (use restore|backup)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
