from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qsl

from ..audit import insert_audit_log
from ..ids import new_id
from ..services._common import InvalidState, iso, utcnow

if TYPE_CHECKING:
    from ..app import ChaboApp


SESSION_TTL_DAYS = 14
ADMIN_LEVEL_ORDER = {
    "viewer": 10,
    "operator": 20,
    "finance": 30,
    "super_admin": 40,
}
PORTAL_STATUSES = {"candidate", "active", "suspended", "revoked"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(
    app: ChaboApp,
    *,
    account_id: str,
    source: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
    impersonator_account_id: str | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, dict[str, Any]]:
    token = secrets.token_urlsafe(32)
    session_id = new_id("sess")
    expires_at = iso(utcnow() + (timedelta(seconds=ttl_seconds) if ttl_seconds else timedelta(days=SESSION_TTL_DAYS)))
    with app.db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO web_sessions (
                id, account_id, token_hash, source, user_agent, ip_address,
                impersonator_account_id, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                account_id,
                hash_token(token),
                source,
                user_agent,
                ip_address,
                impersonator_account_id,
                expires_at,
            ),
        )
        session = dict(conn.execute("SELECT * FROM web_sessions WHERE id = ?", (session_id,)).fetchone())
    return token, session


def load_session(app: ChaboApp, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    token_hash = hash_token(token)
    with app.db.transaction() as conn:
        row = conn.execute(
            """
            SELECT s.*, a.telegram_user_id, a.role, a.display_name
            FROM web_sessions s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.token_hash = ?
              AND s.revoked_at IS NULL
              AND datetime(s.expires_at) > datetime('now')
            """,
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE web_sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
        return dict(row)


def active_portals(conn: sqlite3.Connection, account_id: str) -> list[str]:
    return [
        row["portal"]
        for row in conn.execute(
            """
            SELECT portal FROM portal_access
            WHERE account_id = ? AND status = 'active'
            ORDER BY CASE portal WHEN 'admin' THEN 0 WHEN 'advertiser' THEN 1 ELSE 2 END
            """,
            (account_id,),
        ).fetchall()
    ]


def portal_statuses(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT portal, status, grant_reason, granted_at, revoked_at, updated_at
            FROM portal_access
            WHERE account_id = ?
            ORDER BY CASE portal WHEN 'admin' THEN 0 WHEN 'advertiser' THEN 1 ELSE 2 END
            """,
            (account_id,),
        ).fetchall()
    ]


def admin_level(conn: sqlite3.Connection, account_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM portal_access
        WHERE account_id = ? AND portal = 'admin' AND status = 'active'
        """,
        (account_id,),
    ).fetchone()
    if not row:
        return None
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        return "viewer"
    if "admin_level" not in metadata:
        return "super_admin"
    level = str(metadata.get("admin_level") or "")
    return level if level in ADMIN_LEVEL_ORDER else "viewer"


def sync_portal_access_from_activity(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    ensure_login_candidates: bool = False,
) -> dict[str, Any]:
    """Promote candidate portals once the account has real marketplace activity."""
    status_by_portal = {
        row["portal"]: row["status"]
        for row in conn.execute(
            "SELECT portal, status FROM portal_access WHERE account_id = ?",
            (account_id,),
        ).fetchall()
    }
    advertiser_activity = conn.execute(
        """
        SELECT
            COUNT(*) AS total_orders,
            COALESCE(SUM(CASE
                WHEN o.approved_at IS NOT NULL
                  OR o.status IN ('approved', 'running', 'done', 'budget_exhausted', 'refunded')
                  OR o.spent_cents > 0
                THEN 1 ELSE 0 END), 0) AS successful_orders
        FROM ad_orders o
        WHERE o.advertiser_account_id = ?
        """,
        (account_id,),
    ).fetchone()
    publisher_activity = conn.execute(
        """
        SELECT
            COUNT(DISTINCT c.id) AS owned_channels,
            COUNT(d.id) AS produced_deliveries
        FROM channels c
        LEFT JOIN deliveries d
          ON d.channel_id = c.id
         AND (
             d.status IN ('sent', 'confirmed', 'disputed', 'refunded')
             OR d.charge_cents > 0
         )
        WHERE c.owner_account_id = ?
        """,
        (account_id,),
    ).fetchone()
    promoted: list[str] = []

    advertiser_status = status_by_portal.get("advertiser")
    if advertiser_status not in {"active", "suspended", "revoked"}:
        if advertiser_activity["successful_orders"] > 0:
            grant_portal(
                conn,
                account_id=account_id,
                portal="advertiser",
                status="active",
                reason="auto_advertiser_successful_order",
            )
            promoted.append("advertiser")
        elif ensure_login_candidates and advertiser_status is None:
            grant_portal(
                conn,
                account_id=account_id,
                portal="advertiser",
                status="candidate",
                reason="telegram_webapp_login",
            )

    publisher_status = status_by_portal.get("publisher")
    if publisher_status not in {"active", "suspended", "revoked"}:
        if publisher_activity["produced_deliveries"] > 0:
            grant_portal(
                conn,
                account_id=account_id,
                portal="publisher",
                status="active",
                reason="auto_publisher_produced_delivery",
            )
            promoted.append("publisher")
        elif ensure_login_candidates and publisher_status is None and publisher_activity["owned_channels"] > 0:
            grant_portal(
                conn,
                account_id=account_id,
                portal="publisher",
                status="candidate",
                reason="owns_channel_without_ad_output",
            )

    return {
        "advertiser_successful_orders": int(advertiser_activity["successful_orders"]),
        "advertiser_total_orders": int(advertiser_activity["total_orders"]),
        "publisher_owned_channels": int(publisher_activity["owned_channels"]),
        "publisher_produced_deliveries": int(publisher_activity["produced_deliveries"]),
        "promoted": promoted,
    }


def grant_portal(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    portal: str,
    status: str = "active",
    reason: str = "manual",
    actor_account_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = conn.execute(
        "SELECT * FROM portal_access WHERE account_id = ? AND portal = ?",
        (account_id, portal),
    ).fetchone()
    access_id = current["id"] if current else new_id("pa")
    from_status = current["status"] if current else None
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO portal_access (
            id, account_id, portal, status, grant_reason,
            granted_by_account_id, granted_at, revoked_at, metadata_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(account_id, portal) DO UPDATE SET
            status = excluded.status,
            grant_reason = excluded.grant_reason,
            granted_by_account_id = excluded.granted_by_account_id,
            granted_at = excluded.granted_at,
            revoked_at = NULL,
            metadata_json = excluded.metadata_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (access_id, account_id, portal, status, reason, actor_account_id, metadata_json),
    )
    conn.execute(
        """
        INSERT INTO portal_access_events (
            id, account_id, portal, from_status, to_status, reason, actor_account_id, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id("paev"), account_id, portal, from_status, status, reason, actor_account_id, metadata_json),
    )
    return dict(
        conn.execute(
            "SELECT * FROM portal_access WHERE account_id = ? AND portal = ?",
            (account_id, portal),
        ).fetchone()
    )


def revoke_portal(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    portal: str,
    reason: str = "manual_revoke",
    actor_account_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = conn.execute(
        "SELECT * FROM portal_access WHERE account_id = ? AND portal = ?",
        (account_id, portal),
    ).fetchone()
    access_id = current["id"] if current else new_id("pa")
    from_status = current["status"] if current else None
    if metadata is None and current:
        metadata_json = current["metadata_json"] or "{}"
    else:
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO portal_access (
            id, account_id, portal, status, grant_reason,
            granted_by_account_id, granted_at, revoked_at, metadata_json, updated_at
        )
        VALUES (?, ?, ?, 'revoked', ?, ?, NULL, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(account_id, portal) DO UPDATE SET
            status = 'revoked',
            grant_reason = excluded.grant_reason,
            granted_by_account_id = excluded.granted_by_account_id,
            revoked_at = CURRENT_TIMESTAMP,
            metadata_json = excluded.metadata_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (access_id, account_id, portal, reason, actor_account_id, metadata_json),
    )
    conn.execute(
        """
        INSERT INTO portal_access_events (
            id, account_id, portal, from_status, to_status, reason, actor_account_id, metadata_json
        )
        VALUES (?, ?, ?, ?, 'revoked', ?, ?, ?)
        """,
        (new_id("paev"), account_id, portal, from_status, reason, actor_account_id, metadata_json),
    )
    return dict(
        conn.execute(
            "SELECT * FROM portal_access WHERE account_id = ? AND portal = ?",
            (account_id, portal),
        ).fetchone()
    )


def issue_login_token_conn(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    ttl_seconds: int,
) -> tuple[str, dict[str, Any]]:
    token = secrets.token_urlsafe(32)
    token_id = new_id("login")
    expires_at = iso(utcnow() + timedelta(seconds=ttl_seconds))
    conn.execute(
        """
        INSERT INTO login_tokens (id, account_id, token_hash, purpose, expires_at)
        VALUES (?, ?, ?, 'magic_link', ?)
        """,
        (token_id, account_id, hash_token(token), expires_at),
    )
    row = dict(conn.execute("SELECT * FROM login_tokens WHERE id = ?", (token_id,)).fetchone())
    return token, row


def issue_login_token(app: "ChaboApp", *, account_id: str, ttl_seconds: int | None = None) -> tuple[str, dict[str, Any]]:
    ttl = ttl_seconds if ttl_seconds is not None else app.settings.magic_link_ttl_seconds
    with app.db.transaction() as conn:
        return issue_login_token_conn(conn, account_id=account_id, ttl_seconds=ttl)


def magic_login_path(token: str) -> str:
    return f"/login/magic?token={token}"


def build_magic_link_url(settings: Any, token: str) -> str:
    path = magic_login_path(token)
    base_url = (getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
    return f"{base_url}{path}" if base_url else path


def issue_impersonation_session(
    app: "ChaboApp",
    *,
    admin_account_id: str,
    target_account_id: str,
    portal: str,
    reason: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if portal not in {"advertiser", "publisher"}:
        raise InvalidState("只能代看广告主端或频道主端")
    with app.db.transaction() as conn:
        admin = conn.execute("SELECT * FROM accounts WHERE id = ?", (admin_account_id,)).fetchone()
        target = conn.execute("SELECT * FROM accounts WHERE id = ?", (target_account_id,)).fetchone()
        if not admin:
            raise InvalidState("管理员账号不存在")
        if not target:
            raise InvalidState("目标账号不存在")
        if portal not in active_portals(conn, target_account_id):
            raise InvalidState(f"目标账号未开通 {portal} 端")
        impersonation_id = new_id("imp")
        clean_reason = (reason or "admin_preview").strip()[:200]
        conn.execute(
            """
            INSERT INTO impersonation_sessions (id, admin_account_id, target_account_id, portal, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (impersonation_id, admin_account_id, target_account_id, portal, clean_reason),
        )
        insert_audit_log(
            conn,
            actor_account_id=admin_account_id,
            action="admin_impersonation_started",
            entity_type="account",
            entity_id=target_account_id,
            payload={"portal": portal, "reason": clean_reason, "impersonation_id": impersonation_id},
        )
        impersonation = dict(
            conn.execute("SELECT * FROM impersonation_sessions WHERE id = ?", (impersonation_id,)).fetchone()
        )
    token, session = issue_session(
        app,
        account_id=target_account_id,
        source=f"impersonation:{impersonation_id}",
        user_agent=user_agent,
        ip_address=ip_address,
        impersonator_account_id=admin_account_id,
        ttl_seconds=app.settings.impersonation_session_ttl_seconds,
    )
    return token, session, impersonation


def stop_impersonation_session(
    app: "ChaboApp",
    *,
    session: dict[str, Any],
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, dict[str, Any]]:
    admin_account_id = session.get("impersonator_account_id")
    if not admin_account_id:
        raise InvalidState("当前不是管理员代看会话")
    source = str(session.get("source") or "")
    impersonation_id = source.split(":", 1)[1] if source.startswith("impersonation:") else None
    with app.db.transaction() as conn:
        if impersonation_id:
            conn.execute(
                "UPDATE impersonation_sessions SET ended_at = CURRENT_TIMESTAMP WHERE id = ? AND ended_at IS NULL",
                (impersonation_id,),
            )
        else:
            conn.execute(
                """
                UPDATE impersonation_sessions
                SET ended_at = CURRENT_TIMESTAMP
                WHERE admin_account_id = ? AND target_account_id = ? AND ended_at IS NULL
                """,
                (admin_account_id, session["account_id"]),
            )
        conn.execute("UPDATE web_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE id = ?", (session["id"],))
        insert_audit_log(
            conn,
            actor_account_id=admin_account_id,
            action="admin_impersonation_stopped",
            entity_type="account",
            entity_id=session["account_id"],
            payload={"impersonation_id": impersonation_id},
        )
    return issue_session(
        app,
        account_id=admin_account_id,
        source="impersonation_return",
        user_agent=user_agent,
        ip_address=ip_address,
    )


def consume_login_token(app: ChaboApp, token: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        row = conn.execute(
            """
            SELECT * FROM login_tokens
            WHERE token_hash = ?
              AND consumed_at IS NULL
              AND datetime(expires_at) > datetime('now')
            """,
            (hash_token(token),),
        ).fetchone()
        if not row:
            raise InvalidState("登录链接已失效")
        conn.execute("UPDATE login_tokens SET consumed_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
        account = dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (row["account_id"],)).fetchone())
    return account


def validate_telegram_init_data(init_data: str, *, bot_token: str, max_age_seconds: int) -> dict[str, Any]:
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    received_hash = data.get("hash")
    if not received_hash:
        raise InvalidState("Telegram initData 缺少 hash")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs) if key != "hash")
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise InvalidState("Telegram initData 签名无效")
    try:
        auth_date = int(data["auth_date"])
    except (KeyError, ValueError) as exc:
        raise InvalidState("Telegram initData 缺少 auth_date") from exc
    age = int(utcnow().timestamp()) - auth_date
    if max_age_seconds > 0 and age > max_age_seconds:
        raise InvalidState("Telegram initData 已过期")
    user_raw = data.get("user")
    if not user_raw:
        raise InvalidState("Telegram initData 缺少 user")
    user = json.loads(user_raw)
    if "id" not in user:
        raise InvalidState("Telegram user 缺少 id")
    return {"auth_date": auth_date, "user": user, "raw": data}
