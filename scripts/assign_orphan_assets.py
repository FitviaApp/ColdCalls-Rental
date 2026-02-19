#!/usr/bin/env python3
"""
Assign orphan caller IDs and audios (user_id is null) to a specific user.

Usage:
  python3 scripts/assign_orphan_assets.py --email user@example.com
  python3 scripts/assign_orphan_assets.py --email user@example.com --dry-run
"""
from __future__ import annotations

import argparse
import sys

from app.database import SessionLocal
from app.models import User, CallerID, Audio


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assign orphan assets to a user")
    parser.add_argument("--email", required=True, help="Target non-admin user email")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without changing data")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_email = args.email.strip().lower()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == target_email, User.is_admin == False).first()
        if not user:
            print(f"User not found or is admin: {target_email}")
            return 1

        orphan_caller_ids = db.query(CallerID).filter(CallerID.user_id.is_(None)).count()
        orphan_audios = db.query(Audio).filter(Audio.user_id.is_(None)).count()

        print(f"Target user: {user.email} (id={user.id})")
        print(f"Orphan caller IDs: {orphan_caller_ids}")
        print(f"Orphan audios: {orphan_audios}")

        if args.dry_run:
            print("Dry run only, no changes applied.")
            return 0

        db.query(CallerID).filter(CallerID.user_id.is_(None)).update(
            {CallerID.user_id: user.id},
            synchronize_session=False,
        )
        db.query(Audio).filter(Audio.user_id.is_(None)).update(
            {Audio.user_id: user.id},
            synchronize_session=False,
        )
        db.commit()

        print("Done: orphan assets assigned.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
