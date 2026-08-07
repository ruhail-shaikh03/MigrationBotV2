"""
Run this ONCE, before deploying the RBAC fail-closed default (config.py:Settings.DEFAULT_ROLE),
against the production database — see TDD.md §6.2 and §16.3.

Before this change, get_user_permissions() fell back to role="editor" for any user with no
explicit permissions row. After it, that fallback is settings.DEFAULT_ROLE ("viewer"). Without
this script, every user who has been relying on the old fail-open default loses write access
the moment the new code deploys.

What it does: for every (user, active project) pair that has no existing `permissions` row,
inserts one with role="editor", allowed_fields=["*"], denied_operations=[] — i.e. it makes the
*current* de facto access explicit as real rows, rather than granting anything new. Config
admins (settings.ADMIN_EMAILS) are skipped; admin status doesn't come from a permissions row and
never has.

Idempotent — safe to run more than once. Only inserts where a row is missing.

Usage (from backend/):
    python -m scripts.seed_permissions            # apply
    python -m scripts.seed_permissions --dry-run   # report only, no writes
"""
import asyncio
import argparse
import logging

from sqlalchemy import select

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.models.user import User
from app.models.project import Project
from app.models.permission import Permission

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_permissions")


async def seed_permissions(dry_run: bool = False) -> int:
    admin_emails = set(settings.admin_emails_list)
    created = 0

    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User))).scalars().all()
        projects = (await db.execute(select(Project).where(Project.is_active == True))).scalars().all()
        existing = {
            (p.user_id, p.project_id)
            for p in (await db.execute(select(Permission))).scalars().all()
        }

        logger.info(f"{len(users)} users, {len(projects)} active projects, {len(existing)} existing permission rows.")

        for user in users:
            if user.email.lower().strip() in admin_emails:
                continue  # admin status is settings-driven, not a permissions row

            for project in projects:
                if (user.id, project.id) in existing:
                    continue

                created += 1
                logger.info(
                    f"{'[dry-run] would grant' if dry_run else 'granting'} "
                    f"editor: {user.email} -> project {project.id} ({project.project_name})"
                )
                if not dry_run:
                    db.add(Permission(
                        user_id=user.id,
                        project_id=project.id,
                        role="editor",
                        allowed_fields=["*"],
                        denied_operations=[]
                    ))

        if not dry_run and created:
            await db.commit()

    logger.info(f"{'Would create' if dry_run else 'Created'} {created} permission row(s).")
    return created


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    args = parser.parse_args()
    asyncio.run(seed_permissions(dry_run=args.dry_run))
