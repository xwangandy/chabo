from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..app import ChaboApp
from ..ids import new_id
from ..services._common import InvalidState, NotFound, iso, parse_iso


def list_plans(app: ChaboApp, *, advertiser_account_id: str, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    with app.db.transaction() as conn:
        rows = conn.execute(
            """
            SELECT p.*, cr.text AS creative_text, cr.format_type AS creative_format
            FROM placement_plans p
            LEFT JOIN creatives cr ON cr.id = p.creative_id
            WHERE p.advertiser_account_id = ?
            ORDER BY p.updated_at DESC, p.created_at DESC
            LIMIT ?
            """,
            (advertiser_account_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def create_plan(
    app: ChaboApp,
    *,
    advertiser_account_id: str,
    title: str,
    creative_id: str | None = None,
) -> dict[str, Any]:
    title = (title or "").strip() or "未命名投放计划"
    with app.db.transaction() as conn:
        if creative_id:
            creative = conn.execute(
                """
                SELECT id FROM creatives
                WHERE id = ? AND advertiser_account_id = ? AND archived_at IS NULL
                """,
                (creative_id, advertiser_account_id),
            ).fetchone()
            if not creative:
                raise NotFound(f"广告素材不存在：{creative_id}")
        plan_id = new_id("plan")
        conn.execute(
            """
            INSERT INTO placement_plans (id, advertiser_account_id, creative_id, title)
            VALUES (?, ?, ?, ?)
            """,
            (plan_id, advertiser_account_id, creative_id, title),
        )
    return get_plan(app, advertiser_account_id=advertiser_account_id, plan_id=plan_id)


def get_plan(app: ChaboApp, *, advertiser_account_id: str, plan_id: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        plan = conn.execute(
            """
            SELECT p.*, cr.text AS creative_text, cr.format_type AS creative_format
            FROM placement_plans p
            LEFT JOIN creatives cr ON cr.id = p.creative_id
            WHERE p.id = ? AND p.advertiser_account_id = ?
            """,
            (plan_id, advertiser_account_id),
        ).fetchone()
        if not plan:
            raise NotFound(f"投放计划不存在：{plan_id}")
        items = conn.execute(
            """
            SELECT i.*, c.title AS channel_title, c.username AS channel_username, c.ref_token
            FROM placement_plan_items i
            JOIN channels c ON c.id = i.channel_id
            WHERE i.plan_id = ?
            ORDER BY i.created_at DESC
            """,
            (plan_id,),
        ).fetchall()
        return {**dict(plan), "items": [dict(row) for row in items]}


def add_plan_items(
    app: ChaboApp,
    *,
    advertiser_account_id: str,
    plan_id: str,
    channel_ids: list[str],
    slot_type: str = "standard_card",
    schedule_mode: str = "once",
    starts_at: str | None = None,
    ends_at: str | None = None,
    frequency_per_day: int = 1,
    pin_enabled: bool = False,
) -> dict[str, Any]:
    if not channel_ids:
        raise InvalidState("请选择至少一个频道")
    if schedule_mode not in {"once", "recurring"}:
        raise InvalidState("非法发布模式")
    if frequency_per_day < 1 or frequency_per_day > 24:
        raise InvalidState("每日频率需要在 1 到 24 之间")
    slot_type = app.channels.normalize_slot_type(slot_type)
    starts_at = starts_at or iso()
    total = 0
    with app.db.transaction() as conn:
        plan = conn.execute(
            "SELECT * FROM placement_plans WHERE id = ? AND advertiser_account_id = ?",
            (plan_id, advertiser_account_id),
        ).fetchone()
        if not plan:
            raise NotFound(f"投放计划不存在：{plan_id}")
        for channel_id in dict.fromkeys(channel_ids):
            channel = conn.execute("SELECT id FROM channels WHERE id = ? AND status = 'active'", (channel_id,)).fetchone()
            if not channel:
                continue
            unit_price = 0
            errors: list[str] = []
            policy = conn.execute(
                "SELECT enabled FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = ?",
                (channel_id, slot_type),
            ).fetchone()
            if policy and not policy["enabled"]:
                errors.append("该频道未开启此投放位置")
            try:
                rate = app.channels.get_rate(conn, channel_id, slot_type)
                unit_price = int(rate["unit_price_cents"])
            except Exception:
                errors.append("缺少有效报价")
            estimate = unit_price * _estimate_occurrences(schedule_mode, starts_at, ends_at, frequency_per_day)
            total += estimate if not errors else 0
            conn.execute(
                """
                INSERT INTO placement_plan_items (
                    id, plan_id, channel_id, slot_type, placement_format,
                    schedule_mode, starts_at, ends_at, frequency_per_day,
                    pin_enabled, unit_price_cents, estimated_total_cents,
                    status, validation_errors_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("pli"),
                    plan_id,
                    channel_id,
                    slot_type,
                    slot_type,
                    schedule_mode,
                    starts_at,
                    ends_at,
                    frequency_per_day,
                    1 if pin_enabled else 0,
                    unit_price,
                    estimate,
                    "invalid" if errors else "valid",
                    json.dumps(errors, ensure_ascii=False),
                ),
            )
        summary = _plan_summary(conn, plan_id)
        conn.execute(
            """
            UPDATE placement_plans
            SET total_budget_cents = ?, validation_summary_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (summary["valid_total_cents"], json.dumps(summary, ensure_ascii=False), plan_id),
        )
    return get_plan(app, advertiser_account_id=advertiser_account_id, plan_id=plan_id)


def submit_plan(
    app: ChaboApp,
    *,
    advertiser_account_id: str,
    advertiser_telegram_user_id: str,
    plan_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    with app.db.transaction() as conn:
        plan = conn.execute(
            "SELECT * FROM placement_plans WHERE id = ? AND advertiser_account_id = ?",
            (plan_id, advertiser_account_id),
        ).fetchone()
        if not plan:
            raise NotFound(f"投放计划不存在：{plan_id}")
        if plan["status"] == "submitted":
            raise InvalidState("投放计划已经提交")
        if not plan["creative_id"]:
            raise InvalidState("请先为投放计划选择广告素材")
        creative = conn.execute(
            """
            SELECT id FROM creatives
            WHERE id = ? AND advertiser_account_id = ? AND archived_at IS NULL
            """,
            (plan["creative_id"], advertiser_account_id),
        ).fetchone()
        if not creative:
            raise NotFound(f"广告素材不存在：{plan['creative_id']}")
        items = conn.execute(
            """
            SELECT i.*, c.ref_token
            FROM placement_plan_items i
            JOIN channels c ON c.id = i.channel_id
            WHERE i.plan_id = ? AND i.status = 'valid'
            ORDER BY i.created_at ASC
            """,
            (plan_id,),
        ).fetchall()
        if not items:
            raise InvalidState("投放计划没有可提交的有效频道")
        total_budget = sum(int(item["estimated_total_cents"]) for item in items)
        account = conn.execute(
            "SELECT available_balance_cents FROM accounts WHERE id = ?",
            (advertiser_account_id,),
        ).fetchone()
        if not account or int(account["available_balance_cents"]) < total_budget:
            raise InvalidState("广告钱包余额不足，无法提交整个批量计划")

    created_orders: list[dict[str, Any]] = []
    for item in items:
        order = app.orders.create_order(
            advertiser_telegram_user_id=advertiser_telegram_user_id,
            channel_token=item["ref_token"],
            slot_type=item["slot_type"],
            budget_cents=int(item["estimated_total_cents"]),
            material_id=plan["creative_id"],
            scheduled_at=_parse_optional_datetime(item["starts_at"]),
            end_at=_parse_optional_datetime(item["ends_at"]),
            frequency_per_day=int(item["frequency_per_day"]),
            campaign_name=plan["title"],
            actor_kind="human",
            session_id=session_id,
        )
        created_orders.append(order)

    with app.db.transaction() as conn:
        for item, order in zip(items, created_orders, strict=True):
            conn.execute(
                """
                INSERT OR IGNORE INTO placement_plan_orders (plan_id, plan_item_id, order_id)
                VALUES (?, ?, ?)
                """,
                (plan_id, item["id"], order["id"]),
            )
            conn.execute(
                """
                UPDATE placement_plan_items
                SET status = 'converted', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (item["id"],),
            )
        conn.execute(
            """
            UPDATE placement_plans
            SET status = 'submitted',
                submitted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (plan_id,),
        )
    return {
        **get_plan(app, advertiser_account_id=advertiser_account_id, plan_id=plan_id),
        "created_orders": created_orders,
    }


def _estimate_occurrences(schedule_mode: str, starts_at: str, ends_at: str | None, frequency_per_day: int) -> int:
    if schedule_mode == "once" or not ends_at:
        return 1
    try:
        start = datetime.fromisoformat(starts_at)
        end = datetime.fromisoformat(ends_at)
    except ValueError:
        return 1
    days = max(1, (end.date() - start.date()).days + 1)
    return days * frequency_per_day


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso(value)
    except ValueError as exc:
        raise InvalidState(f"非法时间格式：{value}") from exc


def _plan_summary(conn: Any, plan_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS items_count,
            COALESCE(SUM(CASE WHEN status = 'valid' THEN 1 ELSE 0 END), 0) AS valid_count,
            COALESCE(SUM(CASE WHEN status = 'invalid' THEN 1 ELSE 0 END), 0) AS invalid_count,
            COALESCE(SUM(CASE WHEN status = 'valid' THEN estimated_total_cents ELSE 0 END), 0) AS valid_total_cents
        FROM placement_plan_items
        WHERE plan_id = ?
        """,
        (plan_id,),
    ).fetchone()
    return dict(row)
