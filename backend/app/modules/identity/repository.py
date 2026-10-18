"""身份模块的数据访问层。

`IdentityRepository` 是显式接口（Protocol）：`service.py` 只依赖它，
因此业务规则可以在**不连数据库**的单测里跑（`MemoryIdentityRepository`）。
真实实现 `SqlIdentityRepository` 只做 SQL，不含任何业务判断。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import OwnerCredential, RefreshToken


@dataclass(slots=True, frozen=True)
class Credential:
    subject: str
    email: str
    password_hash: str
    role: str = "owner"
    scopes: tuple[str, ...] = ()
    is_active: bool = True


@dataclass(slots=True)
class RefreshRecord:
    jti: str
    session_id: str
    subject: str
    token_hash: str
    issued_at: datetime
    expires_at: datetime
    parent_jti: str | None = None
    user_agent: str | None = None
    client_ip: str | None = None
    rotated_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


class IdentityRepository(Protocol):
    async def get_credential(self, email: str) -> Credential | None: ...

    async def create_refresh(self, record: RefreshRecord) -> None: ...

    async def find_by_jti(self, jti: str) -> RefreshRecord | None: ...

    async def mark_rotated(self, jti: str, at: datetime) -> bool:
        """原子地把旧令牌标记为已轮换；返回 False 表示它已被别人轮换（并发重放）。"""
        ...

    async def revoke_session(self, session_id: str, reason: str, at: datetime) -> int: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


def _split_scopes(raw: str) -> tuple[str, ...]:
    return tuple(x for x in raw.replace(",", " ").split() if x)


class SqlIdentityRepository:
    """基于 SQLAlchemy AsyncSession 的实现。事务由 service 层显式提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_credential(self, email: str) -> Credential | None:
        row = await self._session.scalar(
            select(OwnerCredential).where(OwnerCredential.email == email.strip().lower())
        )
        if row is None:
            return None
        return Credential(
            subject=row.subject,
            email=row.email,
            password_hash=row.password_hash,
            role=row.role,
            scopes=_split_scopes(row.scopes),
            is_active=row.is_active,
        )

    async def create_refresh(self, record: RefreshRecord) -> None:
        self._session.add(
            RefreshToken(
                id=uuid.uuid4(),
                jti=record.jti,
                session_id=record.session_id,
                subject=record.subject,
                token_hash=record.token_hash,
                parent_jti=record.parent_jti,
                user_agent=record.user_agent,
                client_ip=record.client_ip,
                issued_at=record.issued_at,
                expires_at=record.expires_at,
            )
        )
        await self._session.flush()

    async def find_by_jti(self, jti: str) -> RefreshRecord | None:
        row = await self._session.scalar(select(RefreshToken).where(RefreshToken.jti == jti))
        if row is None:
            return None
        return RefreshRecord(
            jti=row.jti,
            session_id=row.session_id,
            subject=row.subject,
            token_hash=row.token_hash,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            parent_jti=row.parent_jti,
            user_agent=row.user_agent,
            client_ip=row.client_ip,
            rotated_at=row.rotated_at,
            revoked_at=row.revoked_at,
            revoked_reason=row.revoked_reason,
        )

    async def mark_rotated(self, jti: str, at: datetime) -> bool:
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti, RefreshToken.rotated_at.is_(None))
            .values(rotated_at=at)
            .returning(RefreshToken.id)
        )
        return result.first() is not None

    async def revoke_session(self, session_id: str, reason: str, at: datetime) -> int:
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.session_id == session_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=at, revoked_reason=reason)
        )
        return int(result.rowcount or 0)

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class MemoryIdentityRepository:
    """内存实现：单测与本地演示用。与 SQL 实现保持**同样的语义**（含原子轮换）。"""

    def __init__(self, credentials: list[Credential] | None = None) -> None:
        self.credentials = {c.email.lower(): c for c in (credentials or [])}
        self.tokens: dict[str, RefreshRecord] = {}
        self.commits = 0

    async def get_credential(self, email: str) -> Credential | None:
        return self.credentials.get(email.strip().lower())

    async def create_refresh(self, record: RefreshRecord) -> None:
        if record.jti in self.tokens:
            raise ValueError(f"jti 重复: {record.jti}")
        self.tokens[record.jti] = record

    async def find_by_jti(self, jti: str) -> RefreshRecord | None:
        return self.tokens.get(jti)

    async def mark_rotated(self, jti: str, at: datetime) -> bool:
        row = self.tokens.get(jti)
        if row is None or row.rotated_at is not None:
            return False
        row.rotated_at = at
        return True

    async def revoke_session(self, session_id: str, reason: str, at: datetime) -> int:
        count = 0
        for row in self.tokens.values():
            if row.session_id == session_id and row.revoked_at is None:
                row.revoked_at = at
                row.revoked_reason = reason
                count += 1
        return count

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None
