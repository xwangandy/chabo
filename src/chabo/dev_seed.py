from __future__ import annotations

from typing import Any

from .app import ChaboApp
from .money import cents_to_money
from .services._common import utcnow
from .webapi.auth import active_portals, grant_portal


DEMO_PORTALS = ("admin", "advertiser", "publisher")
DEMO_CHANNELS = (
    {
        "telegram_chat_id": "-100900001",
        "title": "H5 冒烟频道 A",
        "username": "chabo_h5_a",
        "category": "software",
        "median_24h_views": 18_000,
        "subscribers": 52_000,
        "light_unique_clickers_30d": 220,
        "repeat_purchase_count": 3,
        "risk_level": "normal",
        "standard_price_cents": 1_200,
    },
    {
        "telegram_chat_id": "-100900002",
        "title": "出海增长频道 B",
        "username": "chabo_growth_b",
        "category": "finance",
        "median_24h_views": 11_500,
        "subscribers": 31_000,
        "light_unique_clickers_30d": 96,
        "repeat_purchase_count": 1,
        "risk_level": "watch",
        "standard_price_cents": 900,
    },
    {
        "telegram_chat_id": "-100900003",
        "title": "工具产品频道 C",
        "username": "chabo_tools_c",
        "category": "general",
        "median_24h_views": 7_800,
        "subscribers": 18_000,
        "light_unique_clickers_30d": 74,
        "repeat_purchase_count": 2,
        "risk_level": "normal",
        "standard_price_cents": 700,
    },
)


def seed_web_demo(
    app: ChaboApp,
    *,
    telegram_user_id: str | int = "10001",
    display_name: str = "H5 冒烟用户",
    min_pending_orders: int = 3,
) -> dict[str, Any]:
    """Create idempotent local data for the React admin/advertiser/publisher portals."""
    user_id = str(telegram_user_id)
    account = _ensure_demo_account(app, user_id, display_name)
    channels = _ensure_demo_channels(app, user_id, display_name)
    topup = _ensure_demo_balance(app, user_id, display_name, target_available_cents=30_000)
    materials = _ensure_demo_materials(app, user_id, display_name)
    orders = _ensure_demo_orders(app, user_id, channels, materials, min_pending_orders)
    topup_request = _ensure_demo_topup_request(app, user_id)
    summary = _demo_counts(app, account["id"], user_id)
    return {
        "account": {
            "id": account["id"],
            "telegram_user_id": account["telegram_user_id"],
            "display_name": account["display_name"],
            "portals": account["portals"],
        },
        "balance_topup": topup,
        "channels": channels,
        "materials": materials,
        "orders_created": orders,
        "topup_request": topup_request,
        "summary": summary,
    }


def _ensure_demo_account(app: ChaboApp, user_id: str, display_name: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        account = app.ledger.accounts.get_or_create_by_telegram(conn, user_id, "mixed", display_name)
        portals = active_portals(conn, account["id"])
        for portal in DEMO_PORTALS:
            if portal not in portals:
                grant_portal(conn, account_id=account["id"], portal=portal, status="active", reason="dev_seed")
        portals = active_portals(conn, account["id"])
    return {**account, "portals": portals}


def _ensure_demo_channels(app: ChaboApp, user_id: str, display_name: str) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    for item in DEMO_CHANNELS:
        channel = app.channels.bind_channel(
            item["telegram_chat_id"],
            item["title"],
            item["username"],
            user_id,
            display_name,
        )
        app.channels.update_rate(channel["id"], "standard_card", item["standard_price_cents"])
        app.channels.update_rate(channel["id"], "button_tail", max(500, item["standard_price_cents"] // 2))
        app.channels.update_rate(channel["id"], "strong_post", item["standard_price_cents"] * 2)
        app.channels.set_daily_ad_limit(channel["id"], 6)
        app.channels.set_format_policy(
            channel["id"],
            "standard_card",
            enabled=True,
            owner_price_band="medium",
            platform_promo_enabled=True,
        )
        app.channels.set_format_policy(
            channel["id"],
            "button_tail",
            enabled=True,
            owner_price_band="low",
            platform_promo_enabled=True,
        )
        app.channels.set_format_policy(
            channel["id"],
            "strong_post",
            enabled=item["risk_level"] == "normal",
            owner_price_band="high",
            platform_promo_enabled=True,
        )
        _ensure_latest_assessment(app, channel["id"], item)
        channels.append(
            {
                "id": channel["id"],
                "title": item["title"],
                "username": item["username"],
                "ref_token": channel["ref_token"],
            }
        )
    return channels


def _ensure_latest_assessment(app: ChaboApp, channel_id: str, item: dict[str, Any]) -> None:
    with app.db.transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM channel_pricing_assessments WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
    if existing:
        return
    app.pricing.assess_channel(
        channel_id=channel_id,
        category=item["category"],
        median_24h_views=item["median_24h_views"],
        subscribers=item["subscribers"],
        light_unique_clickers_30d=item["light_unique_clickers_30d"],
        repeat_purchase_count=item["repeat_purchase_count"],
        risk_level=item["risk_level"],
    )


def _ensure_demo_balance(app: ChaboApp, user_id: str, display_name: str, *, target_available_cents: int) -> dict[str, Any]:
    with app.db.transaction() as conn:
        account = app.ledger.accounts.get_or_create_by_telegram(conn, user_id, "mixed", display_name)
        available = account["available_balance_cents"]
    if available >= target_available_cents:
        return {"added_cents": 0, "available": cents_to_money(available)}
    added = target_available_cents - available
    account = app.ledger.manual_topup(
        user_id,
        added,
        display_name=display_name,
        memo="M6 演示数据：开发期初始余额",
        actor_telegram_user_id=user_id,
        actor_kind="system",
        session_id="dev_seed",
    )
    return {"added_cents": added, "available": cents_to_money(account["available_balance_cents"])}


def _ensure_demo_materials(app: ChaboApp, user_id: str, display_name: str) -> dict[str, dict[str, Any]]:
    materials: dict[str, dict[str, Any]] = {}
    for format_type, payload in {
        "standard_card": {
            "text": "M6 演示标准插播：面向 Telegram 频道的批量投放工作台。",
            "target_url": "https://example.com/chabo-standard",
            "button_text": "查看方案",
            "category": "software",
        },
        "button_tail": {
            "text": "M6 演示按钮插播：只追加按钮入口，用于快速验证详情页和提交链路。",
            "target_url": "https://example.com/chabo-button",
            "button_text": "了解插播",
            "category": "software",
        },
        "strong_post": {
            "text": "M6 演示定制插播：适合预算更高、需要强曝光的广告主。",
            "target_url": "https://example.com/chabo-strong",
            "button_text": "预约投放",
            "category": "software",
        },
    }.items():
        existing = app.materials.list_materials(advertiser_telegram_user_id=user_id, format_type=format_type, limit=1)
        if existing:
            materials[format_type] = existing[0]
            continue
        materials[format_type] = app.materials.create_material(
            advertiser_telegram_user_id=user_id,
            format_type=format_type,
            display_name=display_name,
            actor_kind="system",
            session_id="dev_seed",
            **payload,
        )
    return materials


def _ensure_demo_orders(
    app: ChaboApp,
    user_id: str,
    channels: list[dict[str, Any]],
    materials: dict[str, dict[str, Any]],
    min_pending_orders: int,
) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    material = materials["standard_card"]
    with app.db.transaction() as conn:
        account = conn.execute("SELECT id FROM accounts WHERE telegram_user_id = ?", (user_id,)).fetchone()
        pending = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM ad_orders o
            JOIN campaigns c ON c.id = o.campaign_id
            WHERE o.advertiser_account_id = ?
              AND o.status = 'pending_review'
              AND c.name LIKE 'M6 演示%'
            """,
            (account["id"],),
        ).fetchone()["n"]
    for index in range(max(0, min_pending_orders - pending)):
        channel = channels[index % len(channels)]
        order = app.orders.create_order(
            advertiser_telegram_user_id=user_id,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=2_000,
            campaign_name=f"M6 演示批量投放 {index + pending + 1}",
            scheduled_at=utcnow(),
            actor_kind="system",
            session_id="dev_seed",
        )
        created.append({"id": order["id"], "channel_title": channel["title"], "status": order["status"]})
    return created


def _ensure_demo_topup_request(app: ChaboApp, user_id: str) -> dict[str, Any] | None:
    reason = "M6 演示入账复核"
    with app.db.transaction() as conn:
        existing = conn.execute(
            """
            SELECT * FROM topup_requests
            WHERE recipient_telegram_user_id = ?
              AND status = 'pending'
              AND reason = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, reason),
        ).fetchone()
    if existing:
        return dict(existing)
    return app.topup_approvals.request_topup(
        recipient_telegram_user_id=user_id,
        amount_cents=8_800,
        reason=reason,
        requester_telegram_user_id=user_id,
        evidence_url="https://example.com/chabo-demo-topup",
        request_note="开发期演示：让管理端待审入账列表有稳定样例",
        actor_kind="system",
        session_id="dev_seed",
    )


def _demo_counts(app: ChaboApp, account_id: str, user_id: str) -> dict[str, int]:
    with app.db.transaction() as conn:
        return {
            "channels": conn.execute("SELECT COUNT(*) AS n FROM channels WHERE owner_account_id = ?", (account_id,)).fetchone()["n"],
            "materials": conn.execute("SELECT COUNT(*) AS n FROM creatives WHERE advertiser_account_id = ?", (account_id,)).fetchone()["n"],
            "pending_orders": conn.execute(
                "SELECT COUNT(*) AS n FROM ad_orders WHERE advertiser_account_id = ? AND status = 'pending_review'",
                (account_id,),
            ).fetchone()["n"],
            "pending_topups": conn.execute(
                "SELECT COUNT(*) AS n FROM topup_requests WHERE recipient_telegram_user_id = ? AND status = 'pending'",
                (user_id,),
            ).fetchone()["n"],
        }
