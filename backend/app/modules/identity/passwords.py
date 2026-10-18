"""密码哈希：只用标准库，避免为单用户系统引入 bcrypt/argon2 依赖。

格式 `pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`，自带迭代次数与盐，
将来升级参数时可平滑校验旧哈希（`needs_rehash()`）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(raw: str, *, iterations: int = ITERATIONS) -> str:
    if not raw:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(raw: str, stored: str) -> bool:
    """校验密码。任何格式异常都返回 False（绝不因脏数据抛异常，避免绕过限流）。"""
    try:
        algorithm, iterations_s, salt_hex, digest_hex = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", raw.encode(), bytes.fromhex(salt_hex), int(iterations_s)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def needs_rehash(stored: str) -> bool:
    try:
        algorithm, iterations_s, _, _ = stored.split("$")
    except ValueError:
        return True
    return algorithm != ALGORITHM or int(iterations_s) < ITERATIONS


def hash_token(raw: str) -> str:
    """不透明令牌（刷新令牌）只存哈希。

    与 `core.security.hash_device_token` 同一算法，但刻意不共用符号：
    `core/` 是冻结文件（改动要 x-shared-registry），本窗口不该为了复用一行 sha256 去动它。
    """
    return hashlib.sha256(raw.encode()).hexdigest()
