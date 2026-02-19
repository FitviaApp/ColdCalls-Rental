#!/usr/bin/env python3
"""
Migrate campaign asset ownership to match campaign owners.

What it does:
- Caller IDs:
  - If orphan and used by exactly one user: assign owner.
  - If mismatch: try remap campaign to an existing caller ID owned by campaign user
    with the same phone number.
- Audios:
  - If orphan and used by one user: assign owner.
  - If orphan/mismatch and used by multiple users: clone audio rows per user and remap campaigns.

Usage:
  python3 scripts/migrate_campaign_assets_to_owners.py --dry-run
  python3 scripts/migrate_campaign_assets_to_owners.py
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from app.database import SessionLocal
from app.models import Campaign, CallerID, Audio


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate campaign assets ownership")
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()

    try:
        campaigns = db.query(Campaign).all()

        caller_users = defaultdict(set)
        audio_users = defaultdict(set)
        for c in campaigns:
            caller_users[c.caller_id_id].add(c.user_id)
            audio_users[c.audio_id].add(c.user_id)

        caller_assignments = []
        caller_remaps = []
        caller_unresolved = []

        for caller in db.query(CallerID).all():
            users = caller_users.get(caller.id, set())
            if not users:
                continue

            if caller.user_id is None and len(users) == 1:
                target_user = next(iter(users))
                caller_assignments.append((caller.id, target_user))
                continue

            if caller.user_id is None and len(users) > 1:
                for user_id in users:
                    replacement = db.query(CallerID).filter(
                        CallerID.user_id == user_id,
                        CallerID.phone_number == caller.phone_number,
                    ).first()
                    if replacement:
                        caller_remaps.append((caller.id, replacement.id, user_id))
                    else:
                        caller_unresolved.append((caller.id, caller.phone_number, user_id))
                continue

            # Owned by another user but used by campaigns from different owner.
            for user_id in users:
                if caller.user_id == user_id:
                    continue
                replacement = db.query(CallerID).filter(
                    CallerID.user_id == user_id,
                    CallerID.phone_number == caller.phone_number,
                ).first()
                if replacement:
                    caller_remaps.append((caller.id, replacement.id, user_id))
                else:
                    caller_unresolved.append((caller.id, caller.phone_number, user_id))

        audio_assignments = []
        audio_clones = []

        for audio in db.query(Audio).all():
            users = audio_users.get(audio.id, set())
            if not users:
                continue

            if audio.user_id is None and len(users) == 1:
                target_user = next(iter(users))
                audio_assignments.append((audio.id, target_user))
                continue

            # orphan shared by multiple users or ownership mismatch
            needs_split = (audio.user_id is None and len(users) > 1) or (
                audio.user_id is not None and any(u != audio.user_id for u in users)
            )
            if not needs_split:
                continue

            for user_id in users:
                if audio.user_id == user_id:
                    continue
                existing = db.query(Audio).filter(
                    Audio.user_id == user_id,
                    Audio.r2_key == audio.r2_key,
                    Audio.r2_url == audio.r2_url,
                ).first()
                if existing:
                    audio_clones.append((audio.id, existing.id, user_id, False))
                else:
                    new_audio = Audio(
                        user_id=user_id,
                        name=audio.name,
                        r2_key=audio.r2_key,
                        r2_url=audio.r2_url,
                        duration_seconds=audio.duration_seconds,
                        is_active=audio.is_active,
                    )
                    db.add(new_audio)
                    db.flush()
                    audio_clones.append((audio.id, new_audio.id, user_id, True))

        print(f"Caller assignments: {len(caller_assignments)}")
        print(f"Caller remaps: {len(caller_remaps)}")
        print(f"Caller unresolved: {len(caller_unresolved)}")
        print(f"Audio assignments: {len(audio_assignments)}")
        print(f"Audio remaps/clones: {len(audio_clones)}")

        if caller_unresolved:
            print("\nUnresolved caller ownership conflicts:")
            for caller_id, phone, user_id in caller_unresolved[:50]:
                print(f"- caller_id={caller_id}, phone={phone}, campaign_user_id={user_id}")

        if args.dry_run:
            db.rollback()
            print("\nDry run: no changes applied.")
            return 0

        for caller_id, target_user in caller_assignments:
            db.query(CallerID).filter(CallerID.id == caller_id).update(
                {CallerID.user_id: target_user},
                synchronize_session=False,
            )

        for old_caller_id, new_caller_id, campaign_user_id in caller_remaps:
            db.query(Campaign).filter(
                Campaign.caller_id_id == old_caller_id,
                Campaign.user_id == campaign_user_id,
            ).update({Campaign.caller_id_id: new_caller_id}, synchronize_session=False)

        for audio_id, target_user in audio_assignments:
            db.query(Audio).filter(Audio.id == audio_id).update(
                {Audio.user_id: target_user},
                synchronize_session=False,
            )

        for old_audio_id, new_audio_id, campaign_user_id, _created in audio_clones:
            db.query(Campaign).filter(
                Campaign.audio_id == old_audio_id,
                Campaign.user_id == campaign_user_id,
            ).update({Campaign.audio_id: new_audio_id}, synchronize_session=False)

        db.commit()
        print("\nMigration complete.")

        if caller_unresolved:
            print("\nSome caller conflicts remain unresolved. Create matching caller IDs for those users and rerun.")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
