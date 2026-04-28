from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
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
    admin_token: str | None = None
    webhook_secret: str | None = None
    bot_auto_approve_orders: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        auto_approve = os.getenv("CHABO_BOT_AUTO_APPROVE_ORDERS", "1").strip().lower() not in {"0", "false", "no", "off"}
        return cls(
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
            admin_token=os.getenv("CHABO_ADMIN_TOKEN"),
            webhook_secret=os.getenv("CHABO_WEBHOOK_SECRET"),
            bot_auto_approve_orders=auto_approve,
        )
