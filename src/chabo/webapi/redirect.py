from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from ..app import ChaboApp
from ..ids import new_id
from ..services._common import NotFound

BOT_UA_KEYWORDS = (
    "telegrambot",
    "twitterbot",
    "whatsapp",
    "discordbot",
    "slackbot",
    "facebookexternalhit",
    "linkedinbot",
    "curl/",
    "python-requests",
    "python-urllib",
    "aiohttp",
    "httpx",
    "go-http-client",
    "headlesschrome",
    "phantomjs",
    "playwright",
    "puppeteer",
    "selenium",
)


def is_telegram_native_url(url: str | None) -> bool:
    """Return True if target URL points to a Telegram native resource (Bot/Channel/Group)."""
    raw = (url or "").strip()
    if not raw:
        return True
    if raw.startswith("@") or raw.lower().startswith("tg://"):
        return True
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host in {"t.me", "telegram.me", "telegram.dog"}


def is_prefetch_or_bot_ua(user_agent: str | None) -> bool:
    """Layer 1: Identify link-preview prefetchers and headless/scripted UAs."""
    ua = (user_agent or "").strip().lower()
    if not ua or len(ua) < 8:
        return True
    return any(keyword in ua for keyword in BOT_UA_KEYWORDS)


def resolve_and_record_click(
    app: ChaboApp,
    *,
    token: str,
    client_ip: str | None,
    user_agent: str | None,
) -> dict[str, object]:
    """
    4-Layer Anti-Fraud HTTP Redirect & Click Attribution:
    - Layer 1: Filter TelegramBot / social link-preview prefetchers & scripted UAs
    - Layer 2: Only count clicks on 'sent' or 'confirmed' deliveries
    - Layer 3: 5-minute sliding window deduplication on (delivery_id, ip_hash, ua_hash)
    - Layer 4: Atomic writeback to click_events + deliveries.click_count
    """
    clean_token = (token or "").strip()
    if not clean_token:
        raise NotFound("跳转链接不存在")
    ip_hash = hashlib.sha256((client_ip or "unknown").encode("utf-8")).hexdigest()[:16]
    ua_hash = hashlib.sha256((user_agent or "").encode("utf-8")).hexdigest()[:16]
    is_bot = 1 if is_prefetch_or_bot_ua(user_agent) else 0

    with app.db.transaction() as conn:
        delivery = conn.execute(
            """
            SELECT d.*, cr.target_url
            FROM deliveries d
            JOIN creatives cr ON cr.id = d.creative_id
            WHERE d.tracking_token = ? OR d.id = ?
            LIMIT 1
            """,
            (clean_token, clean_token),
        ).fetchone()
        if not delivery:
            raise NotFound(f"跳转链接不存在或已失效：{clean_token}")

        target_url = str(delivery["target_url"] or "https://t.me")
        delivery_id = str(delivery["id"])
        order_id = str(delivery["order_id"])
        valid_status = str(delivery["status"]) in {"sent", "confirmed"}

        is_unique = 0
        if not is_bot and valid_status:
            recent = conn.execute(
                """
                SELECT id FROM click_events
                WHERE delivery_id = ?
                  AND ip_hash = ?
                  AND ua_hash = ?
                  AND is_bot_prefetch = 0
                  AND created_at >= datetime('now', '-5 minutes')
                LIMIT 1
                """,
                (delivery_id, ip_hash, ua_hash),
            ).fetchone()
            if not recent:
                is_unique = 1

        conn.execute(
            """
            INSERT INTO click_events (
                id, order_id, delivery_id, token, target_url,
                ip_hash, ua_hash, is_bot_prefetch, is_unique
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("clk"),
                order_id,
                delivery_id,
                clean_token,
                target_url,
                ip_hash,
                ua_hash,
                is_bot,
                is_unique,
            ),
        )
        if is_unique:
            conn.execute(
                """
                UPDATE deliveries
                SET click_count = click_count + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (delivery_id,),
            )

    return {
        "delivery_id": delivery_id,
        "order_id": order_id,
        "target_url": target_url,
        "is_bot_prefetch": bool(is_bot),
        "is_unique": bool(is_unique),
    }
