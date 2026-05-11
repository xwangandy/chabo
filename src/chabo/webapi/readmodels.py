from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..app import ChaboApp
from ..money import cents_to_money
from ..services import NotFound


def ops_summary(app: ChaboApp) -> dict[str, int]:
    with app.db.transaction() as conn:
        return {
            "pending_review_orders": conn.execute(
                "SELECT COUNT(*) AS n FROM ad_orders WHERE status = 'pending_review'"
            ).fetchone()["n"],
            "running_orders": conn.execute(
                "SELECT COUNT(*) AS n FROM ad_orders WHERE status = 'running'"
            ).fetchone()["n"],
            "scheduled_due": conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'scheduled' AND scheduled_at <= datetime('now')"
            ).fetchone()["n"],
            "open_disputes": conn.execute(
                "SELECT COUNT(*) AS n FROM disputes WHERE status = 'open'"
            ).fetchone()["n"],
            "pending_topups": conn.execute(
                "SELECT COUNT(*) AS n FROM topup_requests WHERE status = 'pending'"
            ).fetchone()["n"],
        }


def advertiser_dashboard(app: ChaboApp, account_id: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        orders = conn.execute(
            """
            SELECT
                COUNT(*) AS total_orders,
                COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0) AS running_orders,
                COALESCE(SUM(CASE WHEN status = 'pending_review' THEN 1 ELSE 0 END), 0) AS pending_orders
            FROM ad_orders
            WHERE advertiser_account_id = ?
            """,
            (account_id,),
        ).fetchone()
    return {
        "balance": {
            "available": cents_to_money(account["available_balance_cents"]) if account else "0.00",
            "reserved": cents_to_money(account["reserved_balance_cents"]) if account else "0.00",
            "spent": cents_to_money(account["spent_balance_cents"]) if account else "0.00",
        },
        "orders": dict(orders) if orders else {"total_orders": 0, "running_orders": 0, "pending_orders": 0},
    }


def publisher_dashboard(app: ChaboApp, account_id: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        channels = conn.execute(
            "SELECT COUNT(*) AS n FROM channels WHERE owner_account_id = ?",
            (account_id,),
        ).fetchone()["n"]
        deliveries = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM deliveries d
            JOIN channels c ON c.id = d.channel_id
            WHERE c.owner_account_id = ?
            """,
            (account_id,),
        ).fetchone()["n"]
    return {
        "earnings": {
            "pending": cents_to_money(account["pending_earnings_cents"]) if account else "0.00",
            "confirmed": cents_to_money(account["confirmed_earnings_cents"]) if account else "0.00",
            "releasable": cents_to_money(account["releasable_earnings_cents"]) if account else "0.00",
        },
        "channels": channels,
        "deliveries": deliveries,
    }


def channel_market(app: ChaboApp, *, limit: int = 50, offset: int = 0, q: str | None = None) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where = ["c.status = 'active'"]
    params: list[Any] = []
    if q:
        where.append("(c.title LIKE ? OR c.username LIKE ? OR c.ref_token LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    where_sql = " AND ".join(where)
    with app.db.transaction() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM channels c WHERE {where_sql}", params).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT
                c.id, c.title, c.username, c.ref_token, c.status,
                pa.category, pa.score, pa.risk_level,
                MAX(CASE WHEN s.slot_type = 'standard_card' AND rc.active = 1 THEN rc.unit_price_cents END) AS standard_price_cents,
                MAX(CASE WHEN fp.format_type = 'light_tail' THEN fp.enabled ELSE 0 END) AS light_enabled,
                MAX(CASE WHEN fp.format_type = 'standard_card' THEN fp.enabled ELSE 0 END) AS standard_enabled,
                MAX(CASE WHEN fp.format_type = 'strong_post' THEN fp.enabled ELSE 0 END) AS strong_enabled
            FROM channels c
            LEFT JOIN channel_pricing_assessments pa
              ON pa.id = (
                SELECT p2.id FROM channel_pricing_assessments p2
                WHERE p2.channel_id = c.id
                ORDER BY p2.created_at DESC
                LIMIT 1
              )
            LEFT JOIN ad_slots s ON s.channel_id = c.id
            LEFT JOIN rate_cards rc ON rc.slot_id = s.id AND rc.active = 1
            LEFT JOIN channel_ad_format_policies fp ON fp.channel_id = c.id
            WHERE {where_sql}
            GROUP BY c.id
            ORDER BY COALESCE(pa.score, 0) DESC, c.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["light_enabled"] = bool(item["light_enabled"])
        item["standard_enabled"] = bool(item["standard_enabled"])
        item["strong_enabled"] = bool(item["strong_enabled"])
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def admin_orders(app: ChaboApp, *, status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where = []
    params: list[Any] = []
    if status:
        where.append("o.status = ?")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with app.db.transaction() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM ad_orders o {where_sql}", params).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT
                o.id, o.status, o.currency, o.budget_cents, o.reserved_cents, o.spent_cents,
                o.unit_price_cents, o.created_at, o.scheduled_at,
                c.title AS channel_title,
                c.ref_token AS channel_ref_token,
                cr.text AS creative_text,
                a.telegram_user_id AS advertiser_telegram_user_id
            FROM ad_orders o
            JOIN channels c ON c.id = o.channel_id
            JOIN creatives cr ON cr.id = o.creative_id
            JOIN accounts a ON a.id = o.advertiser_account_id
            {where_sql}
            ORDER BY o.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def admin_order_detail(app: ChaboApp, order_id: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        order = conn.execute(
            """
            SELECT o.*, c.title AS channel_title, c.telegram_chat_id, c.ref_token AS channel_ref_token,
                   cr.text AS creative_text, cr.target_url, cr.button_text, cr.format_type,
                   a.telegram_user_id AS advertiser_telegram_user_id
            FROM ad_orders o
            JOIN channels c ON c.id = o.channel_id
            JOIN creatives cr ON cr.id = o.creative_id
            JOIN accounts a ON a.id = o.advertiser_account_id
            WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()
        if not order:
            raise NotFound(f"订单不存在：{order_id}")
        deliveries = conn.execute(
            "SELECT * FROM deliveries WHERE order_id = ? ORDER BY scheduled_at DESC, created_at DESC",
            (order_id,),
        ).fetchall()
        evidence = conn.execute(
            "SELECT * FROM evidence_snapshots WHERE order_id = ? ORDER BY created_at DESC",
            (order_id,),
        ).fetchall()
        ledger = conn.execute(
            "SELECT * FROM ledger_transactions WHERE order_id = ? ORDER BY created_at DESC",
            (order_id,),
        ).fetchall()
        delivery_ids = [row["id"] for row in deliveries]
        audit_rows = _fetch_audit_rows(conn, order_ids=[order_id], delivery_ids=delivery_ids)
        return {
            "order": dict(order),
            "deliveries": [dict(row) for row in deliveries],
            "timeline": _build_timeline(audit_rows, [dict(row) for row in evidence]),
            "ledger_transactions": [dict(row) for row in ledger],
        }


def admin_topups(app: ChaboApp, *, status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where = []
    params: list[Any] = []
    if status:
        where.append("t.status = ?")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with app.db.transaction() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM topup_requests t {where_sql}", params).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT
                t.*, requester.telegram_user_id AS requester_telegram_user_id,
                approver.telegram_user_id AS approver_telegram_user_id
            FROM topup_requests t
            JOIN accounts requester ON requester.id = t.requester_account_id
            LEFT JOIN accounts approver ON approver.id = t.approver_account_id
            {where_sql}
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "retention_days": app.settings.audit_retention_days,
    }


def admin_deliveries(app: ChaboApp, *, status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where = []
    params: list[Any] = []
    if status:
        where.append("d.status = ?")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with app.db.transaction() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM deliveries d {where_sql}", params).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT
                d.*, c.title AS channel_title, o.status AS order_status,
                a.telegram_user_id AS advertiser_telegram_user_id
            FROM deliveries d
            JOIN ad_orders o ON o.id = d.order_id
            JOIN channels c ON c.id = d.channel_id
            JOIN accounts a ON a.id = o.advertiser_account_id
            {where_sql}
            ORDER BY d.scheduled_at DESC, d.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def admin_delivery_detail(app: ChaboApp, delivery_id: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        delivery = conn.execute(
            """
            SELECT d.*, o.status AS order_status, o.budget_cents, o.reserved_cents, o.spent_cents,
                   c.title AS channel_title, c.telegram_chat_id,
                   cr.text AS creative_text, cr.target_url,
                   a.telegram_user_id AS advertiser_telegram_user_id
            FROM deliveries d
            JOIN ad_orders o ON o.id = d.order_id
            JOIN channels c ON c.id = d.channel_id
            JOIN creatives cr ON cr.id = d.creative_id
            JOIN accounts a ON a.id = o.advertiser_account_id
            WHERE d.id = ?
            """,
            (delivery_id,),
        ).fetchone()
        if not delivery:
            raise NotFound(f"投放不存在：{delivery_id}")
        disputes = conn.execute(
            "SELECT * FROM disputes WHERE delivery_id = ? ORDER BY created_at DESC",
            (delivery_id,),
        ).fetchall()
        evidence = conn.execute(
            "SELECT * FROM evidence_snapshots WHERE delivery_id = ? ORDER BY created_at DESC",
            (delivery_id,),
        ).fetchall()
        ledger = conn.execute(
            "SELECT * FROM ledger_transactions WHERE delivery_id = ? ORDER BY created_at DESC",
            (delivery_id,),
        ).fetchall()
        audit_rows = _fetch_audit_rows(
            conn,
            delivery_ids=[delivery_id],
            dispute_ids=[row["id"] for row in disputes],
        )
        return {
            "delivery": dict(delivery),
            "disputes": [dict(row) for row in disputes],
            "timeline": _build_timeline(audit_rows, [dict(row) for row in evidence]),
            "ledger_transactions": [dict(row) for row in ledger],
        }


def admin_disputes(app: ChaboApp, *, status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    disputes = app.disputes.list_disputes(status=status, limit=limit + offset)
    return {
        "items": disputes[offset : offset + limit],
        "total": len(disputes),
        "limit": limit,
        "offset": offset,
    }


def admin_dispute_detail(app: ChaboApp, dispute_id: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        dispute = conn.execute(
            """
            SELECT d.*, c.title AS channel_title, del.status AS delivery_status,
                   del.message_id, o.status AS order_status
            FROM disputes d
            JOIN deliveries del ON del.id = d.delivery_id
            JOIN ad_orders o ON o.id = d.order_id
            JOIN channels c ON c.id = del.channel_id
            WHERE d.id = ?
            """,
            (dispute_id,),
        ).fetchone()
        if not dispute:
            raise NotFound(f"争议不存在：{dispute_id}")
        evidence = conn.execute(
            """
            SELECT *
            FROM evidence_snapshots
            WHERE order_id = ? OR delivery_id = ?
            ORDER BY created_at DESC
            """,
            (dispute["order_id"], dispute["delivery_id"]),
        ).fetchall()
        ledger = conn.execute(
            "SELECT * FROM ledger_transactions WHERE order_id = ? ORDER BY created_at DESC",
            (dispute["order_id"],),
        ).fetchall()
        audit_rows = _fetch_audit_rows(
            conn,
            order_ids=[dispute["order_id"]] if dispute["order_id"] else None,
            delivery_ids=[dispute["delivery_id"]] if dispute["delivery_id"] else None,
            dispute_ids=[dispute_id],
        )
        return {
            "dispute": dict(dispute),
            "timeline": _build_timeline(audit_rows, [dict(row) for row in evidence]),
            "ledger_transactions": [dict(row) for row in ledger],
        }


def admin_accounts(app: ChaboApp, *, q: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    where = []
    params: list[Any] = []
    if q:
        where.append("(a.telegram_user_id LIKE ? OR a.display_name LIKE ? OR a.id LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with app.db.transaction() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM accounts a {where_sql}", params).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT a.id, a.telegram_user_id, a.role, a.active_role, a.display_name,
                   a.available_balance_cents, a.reserved_balance_cents, a.spent_balance_cents,
                   a.pending_earnings_cents, a.confirmed_earnings_cents, a.releasable_earnings_cents,
                   MAX(CASE WHEN pa.portal = 'admin' THEN pa.status END) AS admin_portal_status,
                   MAX(CASE WHEN pa.portal = 'advertiser' THEN pa.status END) AS advertiser_portal_status,
                   MAX(CASE WHEN pa.portal = 'publisher' THEN pa.status END) AS publisher_portal_status,
                   MAX(CASE WHEN pa.portal = 'admin' THEN pa.metadata_json END) AS admin_portal_metadata_json,
                   a.created_at, a.updated_at
            FROM accounts a
            LEFT JOIN portal_access pa ON pa.account_id = a.id
            {where_sql}
            GROUP BY a.id
            ORDER BY a.updated_at DESC, a.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        metadata_raw = item.pop("admin_portal_metadata_json", None)
        item["admin_level"] = None
        if item.get("admin_portal_status") == "active":
            try:
                metadata = json.loads(metadata_raw or "{}")
            except json.JSONDecodeError:
                metadata = {}
            item["admin_level"] = metadata.get("admin_level") or "super_admin"
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def admin_channels(app: ChaboApp, *, q: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    where = []
    params: list[Any] = []
    if q:
        where.append("(c.title LIKE ? OR c.username LIKE ? OR c.ref_token LIKE ? OR c.telegram_chat_id LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with app.db.transaction() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM channels c {where_sql}", params).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT c.id, c.telegram_chat_id, c.title, c.username, c.ref_token, c.status,
                   owner.telegram_user_id AS owner_telegram_user_id,
                   cfg.daily_ad_limit,
                   COUNT(d.id) AS deliveries_count,
                   COALESCE(SUM(d.publisher_net_cents - d.publisher_reversed_cents), 0) AS pending_earnings_cents,
                   c.updated_at
            FROM channels c
            JOIN accounts owner ON owner.id = c.owner_account_id
            LEFT JOIN channel_configs cfg ON cfg.channel_id = c.id
            LEFT JOIN deliveries d ON d.channel_id = c.id
            {where_sql}
            GROUP BY c.id
            ORDER BY c.updated_at DESC, c.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def admin_wallet(app: ChaboApp, *, limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    with app.db.transaction() as conn:
        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(available_balance_cents), 0) AS available_cents,
                COALESCE(SUM(reserved_balance_cents), 0) AS reserved_cents,
                COALESCE(SUM(spent_balance_cents), 0) AS spent_cents,
                COALESCE(SUM(pending_earnings_cents), 0) AS pending_earnings_cents,
                COALESCE(SUM(confirmed_earnings_cents), 0) AS confirmed_earnings_cents,
                COALESCE(SUM(releasable_earnings_cents), 0) AS releasable_earnings_cents
            FROM accounts
            """
        ).fetchone()
        topup_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count, COALESCE(SUM(amount_cents), 0) AS amount_cents
            FROM topup_requests
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        ledger_rows = conn.execute(
            """
            SELECT lt.id, lt.type, lt.currency, lt.amount_cents, lt.memo, lt.created_at,
                   lt.order_id, lt.delivery_id,
                   account.telegram_user_id AS account_telegram_user_id,
                   account.display_name AS account_display_name,
                   related.telegram_user_id AS related_telegram_user_id
            FROM ledger_transactions lt
            JOIN accounts account ON account.id = lt.account_id
            LEFT JOIN accounts related ON related.id = lt.related_account_id
            ORDER BY lt.created_at DESC, lt.rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        accounts = conn.execute(
            """
            SELECT id, telegram_user_id, display_name, role,
                   available_balance_cents, reserved_balance_cents, spent_balance_cents,
                   pending_earnings_cents, confirmed_earnings_cents, releasable_earnings_cents,
                   updated_at
            FROM accounts
            WHERE available_balance_cents != 0
               OR reserved_balance_cents != 0
               OR spent_balance_cents != 0
               OR pending_earnings_cents != 0
               OR confirmed_earnings_cents != 0
               OR releasable_earnings_cents != 0
            ORDER BY
                (available_balance_cents + reserved_balance_cents + pending_earnings_cents + releasable_earnings_cents) DESC,
                updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {
        "totals": dict(totals) if totals else {},
        "topups_by_status": [dict(row) for row in topup_rows],
        "recent_ledger": [dict(row) for row in ledger_rows],
        "accounts": [dict(row) for row in accounts],
        "limit": limit,
    }


def admin_settings(app: ChaboApp) -> dict[str, Any]:
    db_path = Path(app.settings.db_path)
    backup_dir = db_path.parent / "backups"
    with app.db.transaction() as conn:
        migrations = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
        audit = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN audit_hash IS NOT NULL THEN 1 ELSE 0 END), 0) AS signed,
                COALESCE(SUM(CASE WHEN audit_hash IS NULL THEN 1 ELSE 0 END), 0) AS unsigned,
                MIN(created_at) AS first_at,
                MAX(created_at) AS last_at
            FROM audit_logs
            """
        ).fetchone()
        last_backup = None
        if backup_dir.exists():
            backups = sorted(backup_dir.glob("*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
            if backups:
                last_backup = {
                    "path": str(backups[0]),
                    "size_bytes": backups[0].stat().st_size,
                }
    return {
        "environment": app.settings.environment,
        "public_base_url": app.settings.public_base_url,
        "web_allowed_origins": list(app.settings.web_allowed_origins),
        "session_cookie_secure": app.settings.session_cookie_secure,
        "session_cookie_samesite": app.settings.session_cookie_samesite,
        "dev_auth_bypass": app.settings.dev_auth_bypass,
        "dev_session_enabled": app.settings.dev_session_enabled,
        "audit_retention_days": app.settings.audit_retention_days,
        "audit_export_max_rows": app.settings.audit_export_max_rows,
        "magic_link_ttl_seconds": app.settings.magic_link_ttl_seconds,
        "impersonation_session_ttl_seconds": app.settings.impersonation_session_ttl_seconds,
        "database": {
            "path": str(db_path),
            "backup_dir": str(backup_dir),
            "last_backup": last_backup,
            "migrations": migrations,
        },
        "audit": dict(audit) if audit else {},
        "release_gates": [
            {
                "key": "dev_auth_bypass",
                "ok": not app.settings.dev_auth_bypass or not app.settings.is_production,
                "label": "生产关闭开发免登录",
            },
            {
                "key": "session_cookie_secure",
                "ok": app.settings.session_cookie_secure or not app.settings.is_production,
                "label": "生产启用安全 Cookie",
            },
            {
                "key": "audit_retention",
                "ok": app.settings.audit_retention_days >= 365,
                "label": "审计保留不少于 365 天",
            },
            {
                "key": "audit_export_limit",
                "ok": 1 <= app.settings.audit_export_max_rows <= 5000,
                "label": "审计导出上限受控",
            },
        ],
    }


def admin_audit_logs(
    app: ChaboApp,
    *,
    q: str | None = None,
    entity_type: str | None = None,
    category: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    max_limit: int = 100,
) -> dict[str, Any]:
    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)
    where = []
    params: list[Any] = []
    if entity_type:
        where.append("a.entity_type = ?")
        params.append(entity_type)
    if category == "permission":
        where.append("a.action = 'admin_portal_access_updated'")
    elif category == "impersonation":
        where.append("a.action IN ('admin_impersonation_started', 'admin_impersonation_stopped')")
    elif category == "admin_level":
        where.append("a.action = 'admin_portal_access_updated' AND (a.payload_json LIKE ? OR a.payload_json LIKE ?)")
        params.append('%"portal": "admin"%')
        params.append('%"portal":"admin"%')
    if actor:
        where.append("(a.actor_account_id LIKE ? OR actor.telegram_user_id LIKE ? OR actor.display_name LIKE ?)")
        like = f"%{actor}%"
        params.extend([like, like, like])
    if target:
        where.append("(a.entity_id LIKE ? OR target.telegram_user_id LIKE ? OR target.display_name LIKE ?)")
        like = f"%{target}%"
        params.extend([like, like, like])
    if created_from:
        where.append("a.created_at >= ?")
        params.append(created_from)
    if created_to:
        where.append("a.created_at <= ?")
        params.append(created_to)
    if q:
        where.append(
            "(a.action LIKE ? OR a.entity_id LIKE ? OR actor.telegram_user_id LIKE ? "
            "OR target.telegram_user_id LIKE ? OR a.payload_json LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with app.db.transaction() as conn:
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM audit_logs a
            LEFT JOIN accounts actor ON actor.id = a.actor_account_id
            LEFT JOIN accounts target ON target.id = a.entity_id AND a.entity_type = 'account'
            {where_sql}
            """,
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT a.*, actor.telegram_user_id AS actor_telegram_user_id,
                   target.telegram_user_id AS target_telegram_user_id,
                   target.display_name AS target_display_name
            FROM audit_logs a
            LEFT JOIN accounts actor ON actor.id = a.actor_account_id
            LEFT JOIN accounts target ON target.id = a.entity_id AND a.entity_type = 'account'
            {where_sql}
            ORDER BY a.created_at DESC, a.rowid DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        chain_head = conn.execute(
            """
            SELECT audit_hash
            FROM audit_logs
            WHERE audit_hash IS NOT NULL
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "retention_days": app.settings.audit_retention_days,
        "chain_head": chain_head["audit_hash"] if chain_head else None,
    }


def advertiser_orders(app: ChaboApp, *, telegram_user_id: str, status: str | None = None, limit: int = 50) -> dict[str, Any]:
    items = app.orders.list_orders(advertiser_telegram_user_id=telegram_user_id, status=status, limit=limit)
    return {"items": items, "total": len(items), "limit": limit, "offset": 0}


def publisher_channels(app: ChaboApp, *, telegram_user_id: str) -> dict[str, Any]:
    items = app.channels.list_publisher_channels(publisher_telegram_user_id=telegram_user_id)
    return {"items": items, "total": len(items), "limit": len(items), "offset": 0}


def publisher_channel_deliveries(
    app: ChaboApp,
    *,
    telegram_user_id: str,
    channel_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    with app.db.transaction() as conn:
        account = conn.execute(
            "SELECT id FROM accounts WHERE telegram_user_id = ?",
            (str(telegram_user_id),),
        ).fetchone()
        channel = conn.execute(
            "SELECT id FROM channels WHERE id = ?",
            (channel_id,),
        ).fetchone()
        if not account or not channel:
            raise NotFound(f"频道不存在：{channel_id}")
        owned = conn.execute(
            "SELECT id FROM channels WHERE id = ? AND owner_account_id = ?",
            (channel_id, account["id"]),
        ).fetchone()
        if not owned:
            raise NotFound(f"频道不存在：{channel_id}")
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()["n"]
        rows = conn.execute(
            """
            SELECT d.id, d.order_id, d.status, d.scheduled_at, d.sent_at, d.message_id,
                   d.charge_cents, d.publisher_net_cents, d.refunded_cents,
                   cr.text AS creative_text,
                   advertiser.telegram_user_id AS advertiser_telegram_user_id
            FROM deliveries d
            JOIN ad_orders o ON o.id = d.order_id
            JOIN creatives cr ON cr.id = d.creative_id
            JOIN accounts advertiser ON advertiser.id = o.advertiser_account_id
            WHERE d.channel_id = ?
            ORDER BY d.scheduled_at DESC, d.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (channel_id, limit, offset),
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def _fetch_audit_rows(
    conn: sqlite3.Connection,
    *,
    order_ids: list[str] | None = None,
    delivery_ids: list[str] | None = None,
    dispute_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if order_ids:
        placeholders = ",".join("?" for _ in order_ids)
        clauses.append(f"(entity_type = 'ad_order' AND entity_id IN ({placeholders}))")
        params.extend(order_ids)
    if delivery_ids:
        placeholders = ",".join("?" for _ in delivery_ids)
        clauses.append(f"(entity_type = 'delivery' AND entity_id IN ({placeholders}))")
        params.extend(delivery_ids)
    if dispute_ids:
        placeholders = ",".join("?" for _ in dispute_ids)
        clauses.append(f"(entity_type = 'dispute' AND entity_id IN ({placeholders}))")
        params.extend(dispute_ids)
    if not clauses:
        return []
    rows = conn.execute(
        f"SELECT * FROM audit_logs WHERE {' OR '.join(clauses)} ORDER BY created_at DESC",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _build_timeline(
    audit_rows: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in audit_rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {"raw": row.get("payload_json") or ""}
        summary_bits = []
        for key in ("reason", "resolution"):
            if payload.get(key):
                summary_bits.append(f"{key}={payload[key]}")
        if "refund_cents" in payload:
            summary_bits.append(f"refund_cents={payload['refund_cents']}")
        events.append(
            {
                "kind": "操作",
                "at": row.get("created_at") or "",
                "title": row.get("action") or "",
                "entity": f"{row.get('entity_type')}#{row.get('entity_id')}",
                "actor": row.get("actor_account_id") or "(未指定)",
                "note": payload.get("note") or "",
                "summary": "；".join(summary_bits),
            }
        )
    for row in evidence_rows:
        entity = f"order#{row.get('order_id')}" if row.get("order_id") else f"delivery#{row.get('delivery_id')}"
        events.append(
            {
                "kind": "证据",
                "at": row.get("created_at") or "",
                "title": row.get("snapshot_type") or "",
                "entity": entity,
                "actor": "",
                "note": "",
                "summary": (row.get("payload_json") or "")[:160],
            }
        )
    events.sort(key=lambda event: event["at"], reverse=True)
    return events
