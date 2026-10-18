"""集中配置。

规则（不可破例）：
1. 所有配置只来自环境变量，代码里不出现任何真实密钥；
2. 启动时集中校验，缺失/非法立即失败（fail fast），不允许带病启动；
3. 生产环境（APP_ENV=prod）的额外约束在 `_validate_prod()` 中强制。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 运行时 ---
    app_env: Literal["dev", "test", "staging", "prod"] = "dev"
    app_name: str = "agentscontrol-api"
    app_version: str = "0.1.0"
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    api_prefix: str = "/api/v1"

    # --- 数据库 / 缓存 ---
    database_url: str = "postgresql+asyncpg://afc:afc@localhost:5432/afc"
    database_url_sync: str = ""  # 供 Alembic 使用；留空则由 database_url 推导
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_pool_max_overflow: int = Field(default=20, ge=0, le=200)
    db_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"

    # --- 认证 ---
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=60 * 60 * 24 * 14, ge=3600)
    # 单用户系统：只允许白名单内的 owner 登录，不开放注册
    owner_openids: str = ""  # 逗号分隔
    owner_email: str = "owner@example.com"

    # --- 设备接入 ---
    device_hmac_window_seconds: int = Field(default=300, ge=30, le=3600)
    device_offline_after_seconds: int = Field(default=300, ge=60)
    device_suspect_missed_heartbeats: int = Field(default=3, ge=2, le=10)

    # --- 外部集成 ---
    github_webhook_secret: str = ""
    github_token: str = ""
    github_repo: str = ""
    litellm_base_url: str = ""
    langfuse_base_url: str = ""
    cos_bucket: str = ""

    # --- 可观测 ---
    otel_exporter_otlp_endpoint: str = ""
    metrics_enabled: bool = False

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL 必须使用 postgresql+asyncpg:// 驱动")
        return v

    @model_validator(mode="after")
    def _validate_prod(self) -> Settings:
        if self.app_env != "prod":
            return self

        problems: list[str] = []
        if "change-me" in self.jwt_secret or len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET 生产环境必须 >=32 字符且不得使用默认值")
        if not self.github_webhook_secret:
            problems.append("GITHUB_WEBHOOK_SECRET 生产环境必填")
        if not self.owner_openids:
            problems.append("OWNER_OPENIDS 生产环境必填（单用户白名单）")
        if self.app_env == "prod" and self.db_echo:
            problems.append("DB_ECHO 生产环境必须关闭")
        if problems:
            raise ValueError("生产配置校验失败: " + "; ".join(problems))
        return self

    @property
    def sync_dsn(self) -> str:
        if self.database_url_sync:
            return self.database_url_sync
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def owner_openid_list(self) -> list[str]:
        return [x.strip() for x in self.owner_openids.split(",") if x.strip()]

    @property
    def cors_origins(self) -> list[str]:
        """显式来源，禁止通配符（生产由 Caddy 同源反代，开发供 Vercel/本地预览用）。"""
        if self.app_env == "prod":
            return ["https://app.pcxx.dev", "https://preview.pcxx.dev"]
        return ["http://localhost:3000", "http://127.0.0.1:3000"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
