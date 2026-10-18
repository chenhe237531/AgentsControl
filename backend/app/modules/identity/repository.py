"""身份模块的数据访问层。

`IdentityRepository` 是显式接口（Protocol）：`service.py` 只依赖它，
因此业务规则可以在**不连数据库**的单测里跑（`MemoryIdentityRepository`）。
真实实现 `SqlIdentityRepository` 只做 SQL，不含任何业务判断。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuthEvent, OwnerCredential, RefreshToken


@dataclass(slots=True, frozen=True)
class Credential:
    subject: str
    email: str
    password_hash: str
    role: str = "owner"
    scopes: tuple[str, ...] = ()
    is_active: bool = True


@dataclass(slots=True, frozen=True)
class AuthEventRecord:
    """一条认证事件。`event` 必须在 `models.AUTH_EVENT_KINDS` 白名单内。"""

    subject: str
    event: str
    session_id: str | None = None
    detail: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class SessionRecord:
    """一个会话（= `refresh_tokens` 里同一 `session_id` 的整条链）的聚合视图。

    刻意不包含 `token_hash` / `jti`：会话列表是给人看「我在哪些设备上登录过」的，
    不需要（也不应该）把令牌线索暴露到 HTTP 层。
    """

    session_id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None
    user_agent: str | None
    client_ip: str | None
    token_count: int
    revoked_count: int

    def is_active(self, now: datetime) -> bool:
        """仍有未吊销的令牌、且链上最新一枚还没过期 → 会话仍可用。"""
        return self.revoked_count < self.token_count and self.expires_at > now


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

    async def revoke_subject_sessions(
        self,
        subject: str,
        reason: str,
        at: datetime,
        *,
        except_session_id: str | None = None,
    ) -> list[str]:
        """吊销该主体名下**全部**未吊销会话，返回被吊销的 `session_id` 列表。

        `except_session_id` 用于「吊销其它设备」而保留当前会话。
        """
        ...

    async def list_sessions(self, subject: str) -> list[SessionRecord]: ...

    async def record_event(self, record: AuthEventRecord) -> None: ...

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

    async def revoke_subject_sessions(
        self,
        subject: str,
        reason: str,
        at: datetime,
        *,
        except_session_id: str | None = None,
    ) -> list[str]:
        stmt = update(RefreshToken).where(
            RefreshToken.subject == subject, RefreshToken.revoked_at.is_(None)
        )
        if except_session_id:
            stmt = stmt.where(RefreshToken.session_id != except_session_id)
        result = await self._session.execute(
            stmt.values(revoked_at=at, revoked_reason=reason).returning(RefreshToken.session_id)
        )
        return sorted({row[0] for row in result.all()})

    async def list_sessions(self, subject: str) -> list[SessionRecord]:
        # 聚合到 session_id 粒度：一次登录 = 一条链，UI 上一行就够。
        # `count(revoked_at)` 只数非 NULL，所以「有没有仍有效的令牌」= token_count > revoked_count。
        stmt = (
            select(
                RefreshToken.session_id,
                func.min(RefreshToken.issued_at).label("created_at"),
                func.max(RefreshToken.issued_at).label("last_seen_at"),
                func.max(RefreshToken.expires_at).label("expires_at"),
                func.max(RefreshToken.revoked_at).label("revoked_at"),
                func.max(RefreshToken.revoked_reason).label("revoked_reason"),
                func.max(RefreshToken.user_agent).label("user_agent"),
                func.max(RefreshToken.client_ip).label("client_ip"),
                func.count(RefreshToken.id).label("token_count"),
                func.count(RefreshToken.revoked_at).label("revoked_count"),
            )
            .where(RefreshToken.subject == subject)
            .group_by(RefreshToken.session_id)
            .order_by(func.max(RefreshToken.issued_at).desc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            SessionRecord(
                session_id=row.session_id,
                created_at=row.created_at,
                last_seen_at=row.last_seen_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                revoked_reason=row.revoked_reason,
                user_agent=row.user_agent,
                client_ip=row.client_ip,
                token_count=int(row.token_count),
                revoked_count=int(row.revoked_count),
            )
            for row in rows
        ]

    async def record_event(self, record: AuthEventRecord) -> None:
        self._session.add(
            AuthEvent(
                id=uuid.uuid4(),
                subject=record.subject,
                session_id=record.session_id,
                event=record.event,
                detail=record.detail,
                client_ip=record.client_ip,
                user_agent=record.user_agent,
                created_at=record.created_at or datetime.now(UTC),
            )
        )
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class MemoryIdentityRepository:
    """内存实现：单测与本地演示用。与 SQL 实现保持**同样的语义**（含原子轮换）。"""

    def __init__(self, credentials: list[Credential] | None = None) -> None:
        self.credentials = {c.email.lower(): c for c in (credentials or [])}
        self.tokens: dict[str, RefreshRecord] = {}
        self.events: list[AuthEventRecord] = []
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

    async def revoke_subject_sessions(
        self,
        subject: str,
        reason: str,
        at: datetime,
        *,
        except_session_id: str | None = None,
    ) -> list[str]:
        revoked: set[str] = set()
        for row in self.tokens.values():
            if row.subject != subject or row.revoked_at is not None:
                continue
            if except_session_id and row.session_id == except_session_id:
                continue
            row.revoked_at = at
            row.revoked_reason = reason
            revoked.add(row.session_id)
        return sorted(revoked)

    async def list_sessions(self, subject: str) -> list[SessionRecord]:
        grouped: dict[str, list[RefreshRecord]] = {}
        for row in self.tokens.values():
            if row.subject == subject:
                grouped.setdefault(row.session_id, []).append(row)

        sessions = [
            SessionRecord(
                session_id=session_id,
                created_at=min(r.issued_at for r in rows),
                last_seen_at=max(r.issued_at for r in rows),
                expires_at=max(r.expires_at for r in rows),
                revoked_at=max((r.revoked_at for r in rows if r.revoked_at), default=None),
                revoked_reason=next((r.revoked_reason for r in rows if r.revoked_reason), None),
                user_agent=max((r.user_agent for r in rows if r.user_agent), default=None),
                client_ip=max((r.client_ip for r in rows if r.client_ip), default=None),
                token_count=len(rows),
                revoked_count=sum(1 for r in rows if r.revoked_at is not None),
            )
            for session_id, rows in grouped.items()
        ]
        return sorted(sessions, key=lambda s: s.last_seen_at, reverse=True)

    async def record_event(self, record: AuthEventRecord) -> None:
        self.events.append(
            AuthEventRecord(
                subject=record.subject,
                event=record.event,
                session_id=record.session_id,
                detail=record.detail,
                client_ip=record.client_ip,
                user_agent=record.user_agent,
                created_at=record.created_at or datetime.now(UTC),
            )
        )

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None
