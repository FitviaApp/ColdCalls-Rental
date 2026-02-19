#!/usr/bin/env python3
"""
Cleanup legacy billing/credits schema artifacts.

Targets:
- tables: payments, system_settings
- columns: users.credits, campaigns.reserved_credits

Behavior:
- Dry run by default (no changes).
- On --apply:
  - Creates archive copies of legacy tables before dropping.
  - Attempts to drop obsolete columns (SQLite 3.35+ supports DROP COLUMN).
"""
from __future__ import annotations

import argparse
from datetime import datetime

from sqlalchemy import inspect, text

from app.database import engine

LEGACY_TABLES = ("payments", "system_settings")
LEGACY_COLUMNS = (
    ("users", "credits"),
    ("campaigns", "reserved_credits"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cleanup legacy schema objects")
    parser.add_argument("--apply", action="store_true", help="Apply schema changes")
    parser.add_argument("--archive-prefix", default="legacy_archive", help="Prefix for archive tables")
    return parser.parse_args()


def table_exists(name: str) -> bool:
    return name in inspect(engine).get_table_names()


def column_exists(table: str, column: str) -> bool:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def main() -> int:
    args = parse_args()
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    existing_legacy_tables = [t for t in LEGACY_TABLES if table_exists(t)]
    existing_legacy_columns = [(t, c) for (t, c) in LEGACY_COLUMNS if column_exists(t, c)]

    print("Legacy schema cleanup report")
    print(f"- apply mode: {args.apply}")
    print(f"- legacy tables found: {existing_legacy_tables or 'none'}")
    print(f"- legacy columns found: {existing_legacy_columns or 'none'}")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to execute changes.")
        return 0

    with engine.begin() as conn:
        # 1) Archive and drop legacy tables
        for table_name in existing_legacy_tables:
            archive_table = f"{args.archive_prefix}_{table_name}_{timestamp}"
            conn.execute(text(f"CREATE TABLE {archive_table} AS SELECT * FROM {table_name}"))
            conn.execute(text(f"DROP TABLE {table_name}"))
            print(f"Archived and dropped table: {table_name} -> {archive_table}")

        # 2) Drop obsolete columns if supported by SQLite/runtime DB
        for table_name, column_name in existing_legacy_columns:
            try:
                conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))
                print(f"Dropped column: {table_name}.{column_name}")
            except Exception as exc:  # pragma: no cover
                print(
                    f"Could not drop column {table_name}.{column_name}: {exc}. "
                    "Column left in place (safe to ignore)."
                )

    print("\nCleanup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
