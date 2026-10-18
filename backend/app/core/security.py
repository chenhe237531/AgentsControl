"""认证/签名的原子能力（不含业务策略）。

策略归属：
- 登录、刷新、RBAC 判定 → `app/modules/identity`（窗口 W01）
- 设备 HMAC 验签 → `app/modules/devices`（窗口 W04）
本文件只提供无状态、可单测的密码学原语，避免各处重复实现。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any, Literal

import jwt

from .config import settings

TokenType = Literal["access", "refresh"]


class TokenError(Exception):
    pass


def create_token(
    subject: str,
    token_type: TokenType,
    *,
    ttl_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    ttl = ttl_seconds if ttl_seconds is not None else (
        settings.access_token_ttl_seconds if token_type == "access" else settings.refresh_token_ttl_seconds
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, *, expect: TokenType | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if expect and payload.get("typ") != expect:
        raise TokenError(f"token type mismatch: expected {expect}")
    return payload


def sign_hmac(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """设备上报签名：hex(hmac_sha256(secret, f"{ts}." + body))。"""
    ts = str(timestamp if timestamp is not None else int(time.time())).encode()
    return hmac.new(secret.encode(), ts + b"." + body, hashlib.sha256).hexdigest()


def verify_hmac(
    secret: str, body: bytes, signature: str, timestamp: int, *, window_seconds: int | None = None
) -> bool:
    window = window_seconds or settings.device_hmac_window_seconds
    if abs(int(time.time()) - timestamp) > window:
        return False
    return hmac.compare_digest(sign_hmac(secret, body, timestamp), signature)


def hash_device_token(raw: str) -> str:
    """设备 token 只存哈希（与用户密码同一原则），泄露库里拿不到明文。"""
    return hashlib.sha256(raw.encode()).hexdigest()
