from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    environment: str = "local"
    db_path: str = "chabo.sqlite3"
    bot_token: str | None = None
    bot_username: str = "ChaBoBot"
    platform_account_id: str = "platform"
    default_service_fee_bps: int = 500
    default_holdback_bps: int = 1000
    default_holdback_days: int = 7
    star_credit_cents: int = 1
    telegram_http_backend: str = "auto"
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    api_host: str = "127.0.0.1"
    api_port: int = 8081
    public_base_url: str = ""
    admin_token: str | None = None
    webhook_secret: str | None = None
    bot_auto_approve_orders: bool = True
    api_secret_key: str | None = None
    session_cookie_name: str = "chabo_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"
    web_allowed_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    api_proxy_headers: bool = False
    api_forwarded_allow_ips: str = "127.0.0.1"
    dev_session_enabled: bool = False
    dev_auth_bypass: bool = False
    dev_auth_telegram_user_id: str = "10001"
    dev_auth_display_name: str = "开发者"
    magic_link_ttl_seconds: int = 600
    impersonation_session_ttl_seconds: int = 3600
    telegram_webapp_max_age_seconds: int = 3600
    audit_retention_days: int = 1095
    audit_export_max_rows: int = 5000

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production"}

    @classmethod
    def from_env(cls) -> "Settings":
        auto_approve = os.getenv("CHABO_BOT_AUTO_APPROVE_ORDERS", "1").strip().lower() not in {"0", "false", "no", "off"}
        return cls(
            environment=os.getenv("CHABO_ENV", cls.environment),
            db_path=os.getenv("CHABO_DB_PATH", cls.db_path),
            bot_token=os.getenv("CHABO_BOT_TOKEN"),
            bot_username=os.getenv("CHABO_BOT_USERNAME", cls.bot_username),
            platform_account_id=os.getenv("CHABO_PLATFORM_ACCOUNT_ID", cls.platform_account_id),
            default_service_fee_bps=int(os.getenv("CHABO_DEFAULT_SERVICE_FEE_BPS", "500")),
            default_holdback_bps=int(os.getenv("CHABO_DEFAULT_HOLDBACK_BPS", "1000")),
            default_holdback_days=int(os.getenv("CHABO_DEFAULT_HOLDBACK_DAYS", cls.default_holdback_days)),
            star_credit_cents=int(os.getenv("CHABO_STAR_CREDIT_CENTS", cls.star_credit_cents)),
            telegram_http_backend=os.getenv("CHABO_TELEGRAM_HTTP_BACKEND", cls.telegram_http_backend),
            web_host=os.getenv("CHABO_WEB_HOST", cls.web_host),
            web_port=int(os.getenv("CHABO_WEB_PORT", cls.web_port)),
            api_host=os.getenv("CHABO_API_HOST", cls.api_host),
            api_port=int(os.getenv("CHABO_API_PORT", cls.api_port)),
            public_base_url=os.getenv("CHABO_PUBLIC_BASE_URL", cls.public_base_url),
            admin_token=os.getenv("CHABO_ADMIN_TOKEN"),
            webhook_secret=os.getenv("CHABO_WEBHOOK_SECRET"),
            bot_auto_approve_orders=auto_approve,
            api_secret_key=os.getenv("CHABO_API_SECRET_KEY"),
            session_cookie_name=os.getenv("CHABO_SESSION_COOKIE_NAME", cls.session_cookie_name),
            session_cookie_secure=_env_bool("CHABO_SESSION_COOKIE_SECURE", cls.session_cookie_secure),
            session_cookie_samesite=os.getenv("CHABO_SESSION_COOKIE_SAMESITE", cls.session_cookie_samesite),
            web_allowed_origins=_env_csv("CHABO_WEB_ALLOWED_ORIGINS", cls.web_allowed_origins),
            api_proxy_headers=_env_bool("CHABO_API_PROXY_HEADERS", cls.api_proxy_headers),
            api_forwarded_allow_ips=os.getenv("CHABO_API_FORWARDED_ALLOW_IPS", cls.api_forwarded_allow_ips),
            dev_session_enabled=_env_bool("CHABO_DEV_SESSION_ENABLED", cls.dev_session_enabled),
            dev_auth_bypass=_env_bool("CHABO_DEV_AUTH_BYPASS", cls.dev_auth_bypass),
            dev_auth_telegram_user_id=os.getenv("CHABO_DEV_AUTH_TELEGRAM_USER_ID", cls.dev_auth_telegram_user_id),
            dev_auth_display_name=os.getenv("CHABO_DEV_AUTH_DISPLAY_NAME", cls.dev_auth_display_name),
            magic_link_ttl_seconds=int(os.getenv("CHABO_MAGIC_LINK_TTL_SECONDS", cls.magic_link_ttl_seconds)),
            impersonation_session_ttl_seconds=int(
                os.getenv("CHABO_IMPERSONATION_SESSION_TTL_SECONDS", cls.impersonation_session_ttl_seconds)
            ),
            telegram_webapp_max_age_seconds=int(
                os.getenv("CHABO_TELEGRAM_WEBAPP_MAX_AGE_SECONDS", cls.telegram_webapp_max_age_seconds)
            ),
            audit_retention_days=int(os.getenv("CHABO_AUDIT_RETENTION_DAYS", cls.audit_retention_days)),
            audit_export_max_rows=int(os.getenv("CHABO_AUDIT_EXPORT_MAX_ROWS", cls.audit_export_max_rows)),
        )
