"""身份业务规则：登录、刷新（旋转）、登出、当前主体。

不可破坏的三条不变量（改代码前先读）：
1. **刷新令牌每次使用都轮换**，旧 jti 立刻标记 `rotated_at`；
2. 旧令牌被再次使用 = 重放 → **吊销整条会话链**（同 `session_id` 全部置 `revoked_at`），
   而不是只拒绝这一次请求；
3. 刷新令牌**只存哈希**，库里拿不到可用的明文。

本层不依赖 FastAPI 的 Request/Response，只接受纯参数，因此可以脱离 HTTP 单测。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt as pyjwt

from app.core.config import settings
from app.core.errors import RateLimitedError, UnauthorizedError
from app.core.security import TokenError, create_token, decode_token

from .passwords import hash_password, hash_token, verify_password
from .repository import Credential, IdentityRepository, RefreshRecord
from .throttle import LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS, Throttle, failure_keys

# 未知邮箱也跑一次同等代价的校验，避免用响应时间枚举账号是否存在。
_DUMMY_HASH = hash_password("not-a-real-password")


@dataclass(slots=True, frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_in: int
    refresh_expires_in: int
    session_id: str
    subject: str
    role: str
    scopes: tuple[str, ...]


class IdentityService:
    def __init__(self, repository: IdentityRepository, throttle: Throttle) -> None:
        self._repo = repository
        self._throttle = throttle

    # ------------------------------------------------------------------ 登录
    async def login(
        self,
        email: str,
        password: str,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenPair:
        email_key, ip_key = failure_keys(email, client_ip)
        await self._assert_not_blocked(email_key, ip_key)

        credential = await self._repo.get_credential(email)
        if credential is None:
            verify_password(password, _DUMMY_HASH)
        verified = credential is not None and credential.is_active and verify_password(
            password, credential.password_hash
        )
        if not verified:
            await self._record_failure(email_key, ip_key)
            raise UnauthorizedError("邮箱或密码错误", code="invalid_credentials")

        await self._throttle.reset(email_key, ip_key)
        return await self._issue(
            subject=credential.subject,
            role=credential.role,
            scopes=credential.scopes,
            session_id=uuid.uuid4().hex,
            email=credential.email,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    # ------------------------------------------------------------------ 刷新
    async def refresh(self, refresh_token: str) -> TokenPair:
        payload = self._decode_refresh(refresh_token)
        jti = str(payload.get("jti") or "")
        session_id = str(payload.get("sid") or "")
        if not jti or not session_id:
            raise UnauthorizedError("刷新令牌缺少会话标识", code="invalid_token")

        record = await self._repo.find_by_jti(jti)
        if record is None or record.token_hash != hash_token(refresh_token):
            raise UnauthorizedError("刷新令牌无效", code="invalid_token")

        if record.revoked_at is not None:
            raise UnauthorizedError("会话已吊销，请重新登录", code="session_revoked")

        now = datetime.now(UTC)
        if record.rotated_at is not None:
            # 重放：这个 jti 已经换过新令牌，又被拿来用 → 整链吊销
            await self._revoke_chain(record.session_id, "refresh_replayed", now)
            raise UnauthorizedError("刷新令牌已被使用，会话已吊销", code="refresh_replayed")

        if record.expires_at <= now:
            raise UnauthorizedError("刷新令牌已过期", code="refresh_expired")

        rotated = await self._repo.mark_rotated(jti, now)
        if not rotated:
            # 并发窗口：另一个请求抢先轮换了同一条记录，同样按重放处理
            await self._revoke_chain(record.session_id, "refresh_replayed", now)
            raise UnauthorizedError("刷新令牌已被使用，会话已吊销", code="refresh_replayed")

        scopes = payload.get("scopes") or []
        pair = await self._issue(
            subject=record.subject,
            role=str(payload.get("role") or "owner"),
            scopes=tuple(scopes) if not isinstance(scopes, str) else tuple(scopes.split()),
            session_id=record.session_id,
            email=str(payload.get("email") or ""),
            parent_jti=jti,
            client_ip=record.client_ip,
            user_agent=record.user_agent,
            commit=False,
        )
        await self._repo.commit()
        return pair

    # ------------------------------------------------------------------ 登出
    async def logout(
        self, *, session_id: str | None = None, refresh_token: str | None = None
    ) -> int:
        sid = session_id
        if refresh_token:
            try:
                sid = str(decode_token(refresh_token, expect="refresh").get("sid") or sid)
            except TokenError:
                # 令牌已过期/损坏也要能登出：回退到 access token 的 sid
                pass
        if not sid:
            raise UnauthorizedError("缺少会话标识", code="invalid_token")
        revoked = await self._repo.revoke_session(sid, "logout", datetime.now(UTC))
        await self._repo.commit()
        return revoked

    # ------------------------------------------------------------------ 内部
    def _decode_refresh(self, token: str) -> dict:
        """先不验签地读 `exp`，把「过期」与「伪造」区分开，再走正式验签。

        为什么不用 `core.security` 的新异常类型：`core/` 是冻结文件，为了一行判断去改
        它不划算（需要 x-shared-registry 流程）。这里只在**不信任**的 payload 上读一个
        时间戳，真正的验签仍由 `decode_token` 完成，安全性不受影响。
        """
        try:
            unverified = pyjwt.decode(
                token, options={"verify_signature": False, "verify_exp": False}
            )
        except pyjwt.PyJWTError as exc:
            raise UnauthorizedError("刷新令牌无效", code="invalid_token") from exc
        if int(unverified.get("exp") or 0) <= int(datetime.now(UTC).timestamp()):
            raise UnauthorizedError("刷新令牌已过期", code="refresh_expired")
        try:
            return decode_token(token, expect="refresh")
        except TokenError as exc:
            raise UnauthorizedError("刷新令牌无效", code="invalid_token") from exc

    async def _issue(
        self,
        *,
        subject: str,
        role: str,
        scopes: tuple[str, ...],
        session_id: str,
        email: str = "",
        parent_jti: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
        commit: bool = True,
    ) -> TokenPair:
        claims = {"role": role, "scopes": list(scopes), "sid": session_id, "email": email}
        access = create_token(subject, "access", extra=claims)
        refresh = create_token(subject, "refresh", extra=claims)
        payload = decode_token(refresh, expect="refresh")

        await self._repo.create_refresh(
            RefreshRecord(
                jti=str(payload["jti"]),
                session_id=session_id,
                subject=subject,
                token_hash=hash_token(refresh),
                issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
                expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
                parent_jti=parent_jti,
                user_agent=(user_agent or None),
                client_ip=(client_ip or None),
            )
        )
        if commit:
            await self._repo.commit()
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            access_expires_in=settings.access_token_ttl_seconds,
            refresh_expires_in=settings.refresh_token_ttl_seconds,
            session_id=session_id,
            subject=subject,
            role=role,
            scopes=scopes,
        )

    async def _revoke_chain(self, session_id: str, reason: str, at: datetime) -> None:
        await self._repo.revoke_session(session_id, reason, at)
        await self._repo.commit()

    async def _assert_not_blocked(self, *keys: str) -> None:
        for key in keys:
            if await self._throttle.get(key) >= LOGIN_MAX_FAILURES:
                raise RateLimitedError(
                    "登录失败次数过多，请稍后再试",
                    details={"retry_after_seconds": LOGIN_WINDOW_SECONDS},
                )

    async def _record_failure(self, *keys: str) -> None:
        for key in keys:
            await self._throttle.hit(key, LOGIN_WINDOW_SECONDS)


def credential_from_plain(email: str, password: str, *, subject: str, role: str = "owner",
                          scopes: tuple[str, ...] = ()) -> Credential:
    """运维脚本/测试播种用：把明文密码转成可入库的 `Credential`。"""
    return Credential(
        subject=subject,
        email=email.strip().lower(),
        password_hash=hash_password(password),
        role=role,
        scopes=scopes,
    )
