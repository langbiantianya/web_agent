"""初始化固定平台 owner superAdmin。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

import sqlalchemy as sa

from app.models.admin_control import SuperAdminAccount
from app.models.base import utcnow
from services.admin_web.accounts import create_super_admin_account
from services.admin_web.db import AdminSessionLocal, dispose_admin_engine
from services.admin_web.security import generate_password, hash_password


async def _run(args: argparse.Namespace) -> int:
    """执行一次性 owner 初始化。"""
    async with AdminSessionLocal() as db:
        owner = await db.scalar(
            sa.select(SuperAdminAccount).where(
                SuperAdminAccount.owner_slot == "superAdmin"
            )
        )
        if owner is not None:
            print("superAdmin owner already exists", file=sys.stderr)
            return 2
        if args.stdout:
            password = generate_password()
            now = utcnow()
            account = SuperAdminAccount(
                username="superAdmin",
                password_hash=hash_password(password),
                email=args.email,
                phone=args.phone,
                status="active",
                owner_slot="superAdmin",
                password_rotated_at=now,
                password_expires_at=now + timedelta(days=30),
                password_version=1,
            )
            db.add(account)
            await db.commit()
            print(password)
            return 0
        if not args.email and not args.phone:
            print("--email or --phone is required unless --stdout is used", file=sys.stderr)
            return 2
        try:
            await create_super_admin_account(
                db,
                username="superAdmin",
                email=args.email,
                phone=args.phone,
                owner=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"bootstrap failed: {type(exc).__name__}", file=sys.stderr)
            return 1
    print("superAdmin created; generated password was sent")
    return 0


def main() -> int:
    """解析 CLI 参数并运行初始化。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--email")
    parser.add_argument("--phone")
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="local bootstrap only: print the generated password once",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    finally:
        asyncio.run(dispose_admin_engine())


if __name__ == "__main__":
    raise SystemExit(main())
