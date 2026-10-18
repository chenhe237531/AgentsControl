"""核心原语单测：安全、错误、配置。"""

from __future__ import annotations

import time

import pytest

from app.core.config import Settings
from app.core.security import (
    TokenError,
    create_token,
    decode_token,
    hash_device_token,
    sign_hmac,
    verify_hmac,
)


def test_access_token_roundtrip() -> None:
    token = create_token("owner-1", "access")
    payload = decode_token(token, expect="access")
    assert payload["sub"] == "owner-1"
    assert payload["typ"] == "access"


def test_token_type_mismatch_rejected() -> None:
    token = create_token("owner-1", "refresh")
    with pytest.raises(TokenError):
        decode_token(token, expect="access")


def test_expired_token_rejected() -> None:
    token = create_token("owner-1", "access", ttl_seconds=-10)
    with pytest.raises(TokenError):
        decode_token(token)


def test_hmac_roundtrip_and_skew() -> None:
    body = b'{"node":"n-1","seq":1}'
    ts = int(time.time())
    sig = sign_hmac("s3cret", body, ts)
    assert verify_hmac("s3cret", body, sig, ts)
    assert not verify_hmac("s3cret", body, sig, ts - 10_000)
    assert not verify_hmac("other", body, sig, ts)


def test_device_token_is_hashed() -> None:
    assert hash_device_token("raw-token") != "raw-token"
    assert len(hash_device_token("raw-token")) == 64


def test_prod_requires_owner_whitelist() -> None:
    with pytest.raises(ValueError) as exc:
        Settings(
            app_env="prod",
            jwt_secret="x" * 40,
            github_webhook_secret="whsec",
            owner_openids="",
        )
    assert "OWNER_OPENIDS" in str(exc.value)
