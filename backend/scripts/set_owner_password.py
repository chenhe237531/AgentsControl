#!/usr/bin/env python3
"""播种/重置 owner 登录凭据（单用户系统，不开放注册）。

用法：
    python scripts/set_owner_password.py --email owner@example.com --subject owner-1
    python scripts/set_owner_password.py --email owner@example.com --subject owner-1 --password 'xxx'

不传 `--password` 时从环境变量 `OWNER_PASSWORD` 读取；都没有则交互式输入（不回显）。

为什么必须有这个脚本：密码哈希只能由代码生成，直接往库里插明文哈希是错误的做法；
运维只需要「能设置一次密码」，不需要一套注册接口。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.db import dispose_engine, get_sessionmaker  # noqa: E402
from app.modules.identity.models import OwnerCredential  # noqa: E402
from app.modules.identity.passwords import hash_password  # noqa: E402


async def _upsert(email: str, subject: str, role: str, scopes: str, password: str) -> str:
    async with get_sessionmaker()() as session:
        row = await session.scalar(
            select(OwnerCredential).where(OwnerCredential.email == email.lower())
        )
        if row is None:
            row = OwnerCredential(
                subject=subject, email=email.lower(), password_hash=hash_password(password),
                role=role, scopes=scopes, is_active=True,
            )
            session.add(row)
            action = "created"
        else:
            row.subject = subject
            row.password_hash = hash_password(password)
            row.role = role
            row.scopes = scopes
            row.is_active = True
            action = "updated"
        await session.commit()
        return action


def main() -> int:
    parser = argparse.ArgumentParser(description="设置 owner 登录密码")
    parser.add_argument("--email", required=True)
    parser.add_argument("--subject", default="owner-1", help="令牌中的 sub，默认 owner-1")
    parser.add_argument("--role", default="owner", choices=["owner", "operator", "viewer"])
    parser.add_argument("--scopes", default="", help="空格或逗号分隔，例如 'tasks:write ledger:read'")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    password = args.password or os.environ.get("OWNER_PASSWORD") or ""
    if not password:
        password = getpass.getpass("新密码：")
    if len(password) < 12:
        print("密码至少 12 位（单用户系统，这是唯一防线）", file=sys.stderr)
        return 2

    action = asyncio.run(_upsert(args.email, args.subject, args.role, args.scopes, password))
    asyncio.run(dispose_engine())
    print(f"凭据已{action}：{args.email} (subject={args.subject}, role={args.role})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
