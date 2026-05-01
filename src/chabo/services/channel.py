from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..db import Database
from ..ids import new_id, new_ref_token
from ..money import bps_amount


logger = logging.getLogger(__name__)

from ._common import (
    ChaboError,
    InsufficientBalance,
    InvalidState,
    NotFound,
    _audit_payload_with_note,
    iso,
    parse_iso,
    row_to_dict,
    utcnow,
)
from .wallet import (
    AccountService,
    LedgerService,
    ToolCallLogService,
)


class ChannelService:
    SLOT_ALIASES = {
        "standard": "standard_card",
        "scheduled": "standard_card",
    }
    DEFAULT_RATES = {
        "light_tail": ("per_tail", 300),
        "button_tail": ("per_button", 500),
        "standard_card": ("per_post", 1_000),
        "strong_post": ("per_post", 1_800),
        "pin24h": ("per_24h_pin", 2_500),
        "loop_daily": ("per_day", 800),
    }

    def __init__(self, db: Database, settings: Settings, gateway: Any | None = None):
        """Optional gateway is used for advertiser notifications.

        When None, set_format_policy / similar publisher-driven changes do
        not push notifications to affected advertisers — keeps internal
        / non-user-facing instances pure.
        """
        self.db = db
        self.settings = settings
        self.gateway = gateway
        self.accounts = AccountService(db, settings)
        self.tool_calls = ToolCallLogService(db, settings)

    def bind_channel(
        self,
        telegram_chat_id: str | int,
        title: str,
        username: str | None,
        owner_telegram_user_id: str | int,
        owner_display_name: str | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            owner = self.accounts.get_or_create_by_telegram(
                conn,
                owner_telegram_user_id,
                "publisher",
                owner_display_name,
            )
            existing = conn.execute(
                "SELECT * FROM channels WHERE telegram_chat_id = ?",
                (str(telegram_chat_id),),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE channels
                    SET title = ?, username = ?, owner_account_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (title, username, owner["id"], existing["id"]),
                )
                channel_id = existing["id"]
            else:
                channel_id = new_id("chan")
                conn.execute(
                    """
                    INSERT INTO channels (id, telegram_chat_id, title, username, ref_token, owner_account_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (channel_id, str(telegram_chat_id), title, username, new_ref_token(), owner["id"]),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO channel_configs (
                    channel_id, service_fee_bps, holdback_bps, holdback_days
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    channel_id,
                    self.settings.default_service_fee_bps,
                    self.settings.default_holdback_bps,
                    self.settings.default_holdback_days,
                ),
            )
            self._ensure_default_rate_cards(conn, channel_id)
            self._ensure_default_format_policies(conn, channel_id)
            self.record_channel_admin(
                conn,
                channel_id=channel_id,
                telegram_user_id=owner_telegram_user_id,
                status="creator",
                display_name=owner_display_name,
                is_bot=False,
            )
            self._audit(conn, owner["id"], "channel_bound", "channel", channel_id, {"title": title})
            return self.get_channel(conn, channel_id)

    def record_channel_admin(
        self,
        conn: sqlite3.Connection,
        *,
        channel_id: str,
        telegram_user_id: str | int,
        status: str,
        display_name: str | None = None,
        is_bot: bool = False,
        can_post_messages: bool = False,
        can_edit_messages: bool = False,
        can_pin_messages: bool = False,
    ) -> dict[str, Any]:
        telegram_user_id = str(telegram_user_id)
        if not is_bot:
            self.accounts.get_or_create_by_telegram(conn, telegram_user_id, "publisher", display_name)
        conn.execute(
            """
            INSERT INTO channel_admins (
                channel_id, telegram_user_id, status, display_name, is_bot,
                can_post_messages, can_edit_messages, can_pin_messages, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id, telegram_user_id) DO UPDATE SET
                status = excluded.status,
                display_name = COALESCE(excluded.display_name, channel_admins.display_name),
                is_bot = excluded.is_bot,
                can_post_messages = excluded.can_post_messages,
                can_edit_messages = excluded.can_edit_messages,
                can_pin_messages = excluded.can_pin_messages,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (
                channel_id,
                telegram_user_id,
                status,
                display_name,
                1 if is_bot else 0,
                1 if can_post_messages else 0,
                1 if can_edit_messages else 0,
                1 if can_pin_messages else 0,
            ),
        )
        row = conn.execute(
            "SELECT * FROM channel_admins WHERE channel_id = ? AND telegram_user_id = ?",
            (channel_id, telegram_user_id),
        ).fetchone()
        return dict(row)

    def sync_channel_admins(self, conn: sqlite3.Connection, channel_id: str, members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        synced: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for member in members:
            user = member.get("user") or {}
            telegram_user_id = user.get("id")
            if not telegram_user_id:
                continue
            display_name = self._display_name_from_user(user)
            status = member.get("status", "administrator")
            row = self.record_channel_admin(
                conn,
                channel_id=channel_id,
                telegram_user_id=telegram_user_id,
                status=status,
                display_name=display_name,
                is_bot=bool(user.get("is_bot")),
                can_post_messages=bool(member.get("can_post_messages")),
                can_edit_messages=bool(member.get("can_edit_messages")),
                can_pin_messages=bool(member.get("can_pin_messages")),
            )
            synced.append(row)
            seen_ids.add(str(telegram_user_id))
        if seen_ids:
            placeholders = ",".join("?" for _ in seen_ids)
            conn.execute(
                f"""
                DELETE FROM channel_admins
                WHERE channel_id = ?
                  AND telegram_user_id NOT IN ({placeholders})
                """,
                (channel_id, *seen_ids),
            )
        return synced

    def list_admin_channels(self, conn: sqlite3.Connection, telegram_user_id: str | int) -> list[dict[str, Any]]:
        telegram_user_id = str(telegram_user_id)
        rows = conn.execute(
            """
            SELECT DISTINCT c.*
            FROM channels c
            LEFT JOIN channel_admins ca
              ON ca.channel_id = c.id
             AND ca.telegram_user_id = ?
             AND ca.status IN ('creator', 'administrator')
            LEFT JOIN accounts owner
              ON owner.id = c.owner_account_id
             AND owner.telegram_user_id = ?
            WHERE c.status = 'active'
              AND (ca.telegram_user_id IS NOT NULL OR owner.telegram_user_id IS NOT NULL)
            ORDER BY c.updated_at DESC, c.created_at DESC
            """,
            (telegram_user_id, telegram_user_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_channel_admins(self, conn: sqlite3.Connection, channel_id: str, *, include_bots: bool = False) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT * FROM channel_admins
            WHERE channel_id = ?
              AND status IN ('creator', 'administrator')
              AND (? = 1 OR is_bot = 0)
            ORDER BY is_bot ASC, display_name ASC, telegram_user_id ASC
            """,
            (channel_id, 1 if include_bots else 0),
        ).fetchall()
        return [dict(row) for row in rows]

    def _display_name_from_user(self, user: dict[str, Any]) -> str | None:
        name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part)
        return name or user.get("username")

    def normalize_slot_type(self, slot_type: str) -> str:
        return self.SLOT_ALIASES.get(slot_type, slot_type)

    def _ensure_default_rate_cards(self, conn: sqlite3.Connection, channel_id: str) -> None:
        for slot_type, (pricing_unit, unit_price_cents) in self.DEFAULT_RATES.items():
            slot_id = new_id("slot")
            conn.execute(
                """
                INSERT OR IGNORE INTO ad_slots (id, channel_id, slot_type)
                VALUES (?, ?, ?)
                """,
                (slot_id, channel_id, slot_type),
            )
            row = conn.execute(
                "SELECT id FROM ad_slots WHERE channel_id = ? AND slot_type = ?",
                (channel_id, slot_type),
            ).fetchone()
            existing_rate = conn.execute(
                "SELECT id FROM rate_cards WHERE slot_id = ? AND active = 1",
                (row["id"],),
            ).fetchone()
            if not existing_rate:
                conn.execute(
                    """
                    INSERT INTO rate_cards (id, slot_id, unit_price_cents, pricing_unit)
                    VALUES (?, ?, ?, ?)
                    """,
                    (new_id("rate"), row["id"], unit_price_cents, pricing_unit),
                )

    def _ensure_default_format_policies(self, conn: sqlite3.Connection, channel_id: str) -> None:
        for format_type in self.DEFAULT_RATES:
            conn.execute(
                """
                INSERT OR IGNORE INTO channel_ad_format_policies (
                    id, channel_id, format_type, enabled, owner_price_band, platform_promo_enabled
                )
                VALUES (?, ?, ?, 1, 'medium', 1)
                """,
                (new_id("pol"), channel_id, format_type),
            )

    def set_format_policy(
        self,
        channel_id: str,
        format_type: str,
        *,
        enabled: bool,
        owner_price_band: str = "medium",
        platform_promo_enabled: bool = True,
        custom_multiplier_bps: int | None = None,
    ) -> dict[str, Any]:
        format_type = self.normalize_slot_type(format_type)
        if format_type not in self.DEFAULT_RATES:
            raise NotFound(f"unknown ad format: {format_type}")
        with self.db.transaction() as conn:
            channel = self.get_channel(conn, channel_id)
            from .billing import SubscriptionService
            has_premium = SubscriptionService.has_active_subscription(conn, channel_id)
            wants_advanced = (not platform_promo_enabled) or owner_price_band == "custom" or custom_multiplier_bps is not None
            if wants_advanced and not has_premium:
                raise InvalidState("该配置属于频道高级功能，需要先开通频道高级订阅")
            self._ensure_default_format_policies(conn, channel_id)
            previous = conn.execute(
                "SELECT enabled, owner_price_band FROM channel_ad_format_policies "
                "WHERE channel_id = ? AND format_type = ?",
                (channel_id, format_type),
            ).fetchone()
            conn.execute(
                """
                UPDATE channel_ad_format_policies
                SET enabled = ?,
                    owner_price_band = ?,
                    platform_promo_enabled = ?,
                    custom_multiplier_bps = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND format_type = ?
                """,
                (
                    1 if enabled else 0,
                    owner_price_band,
                    1 if platform_promo_enabled else 0,
                    custom_multiplier_bps,
                    channel_id,
                    format_type,
                ),
            )
            row = conn.execute(
                "SELECT * FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = ?",
                (channel_id, format_type),
            ).fetchone()
            self._notify_advertisers_on_policy_change(
                conn,
                channel=channel,
                format_type=format_type,
                previous=dict(previous) if previous else None,
                current=dict(row),
            )
            return dict(row)

    def _notify_advertisers_on_policy_change(
        self,
        conn: sqlite3.Connection,
        *,
        channel: dict[str, Any],
        format_type: str,
        previous: dict[str, Any] | None,
        current: dict[str, Any],
    ) -> None:
        """Notify advertisers with open orders on (channel, format) when the
        publisher meaningfully changes the policy (enabled flag flipped or
        price band changed). Silent when gateway absent or no open orders."""
        if not self.gateway:
            return
        if previous is None:
            return  # first-time default insert; not user-driven change
        flipped_enabled = bool(previous.get("enabled")) != bool(current.get("enabled"))
        band_changed = (previous.get("owner_price_band") or "") != (current.get("owner_price_band") or "")
        if not (flipped_enabled or band_changed):
            return

        rows = conn.execute(
            """
            SELECT DISTINCT a.telegram_user_id, a.id AS account_id
            FROM ad_orders o
            JOIN ad_slots s ON s.id = o.slot_id
            JOIN accounts a ON a.id = o.advertiser_account_id
            WHERE o.channel_id = ?
              AND s.slot_type = ?
              AND o.status IN ('pending_review', 'approved', 'running')
              AND a.telegram_user_id IS NOT NULL
            """,
            (channel["id"], format_type),
        ).fetchall()
        if not rows:
            return

        slot_label = {
            "light_tail": "文字插播",
            "button_tail": "按钮插播",
            "standard_card": "标准插播",
            "strong_post": "定制插播",
            "pin24h": "置顶 24h",
            "loop_daily": "循环发布",
        }.get(format_type, format_type)

        change_lines = []
        if flipped_enabled:
            change_lines.append("✅ 已开启" if current.get("enabled") else "❌ 已关闭")
        if band_changed:
            band_label = {"low": "低档", "medium": "中档", "high": "高档", "custom": "自定义"}
            change_lines.append(
                f"💵 价格档：{band_label.get(previous.get('owner_price_band'), previous.get('owner_price_band'))}"
                f" → {band_label.get(current.get('owner_price_band'), current.get('owner_price_band'))}"
            )
        text = (
            "🔔 频道主刚刚修改了设置\n\n"
            f"📺 {channel['title']}\n"
            f"🧩 {slot_label}\n"
            + "\n".join(change_lines)
            + "\n\n你在该频道有未结束的投放，请查看是否需要调整。"
        )
        keyboard = [[{"text": "📣 我的广告", "callback_data": "advertiser:orders"}]]

        for row in rows:
            try:
                self.gateway.send_private_message(
                    chat_id=row["telegram_user_id"],
                    text=text,
                    inline_keyboard=keyboard,
                )
            except Exception as exc:
                logger.warning(
                    "notify_advertiser_policy_change_failed channel=%s format=%s recipient=%s error=%s",
                    channel["id"],
                    format_type,
                    row["telegram_user_id"],
                    exc,
                )

    def _verify_publisher_owns_channel(
        self,
        conn: sqlite3.Connection,
        *,
        publisher_telegram_user_id: str | int,
        channel_id: str,
    ) -> None:
        account = conn.execute(
            "SELECT id FROM accounts WHERE telegram_user_id = ?",
            (str(publisher_telegram_user_id),),
        ).fetchone()
        if not account:
            raise NotFound(f"频道不存在：{channel_id}")
        channel = conn.execute(
            "SELECT owner_account_id FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()
        if not channel or channel["owner_account_id"] != account["id"]:
            raise NotFound(f"频道不存在：{channel_id}")

    def set_format_policy_for_publisher(
        self,
        *,
        publisher_telegram_user_id: str | int,
        channel_id: str,
        format_type: str,
        enabled: bool,
        owner_price_band: str = "medium",
        platform_promo_enabled: bool = True,
        custom_multiplier_bps: int | None = None,
        actor_kind: str = "human",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {
            "channel_id": channel_id,
            "format_type": format_type,
            "enabled": enabled,
            "owner_price_band": owner_price_band,
            "platform_promo_enabled": platform_promo_enabled,
        }
        try:
            with self.db.transaction() as conn:
                self._verify_publisher_owns_channel(
                    conn,
                    publisher_telegram_user_id=publisher_telegram_user_id,
                    channel_id=channel_id,
                )
            policy = self.set_format_policy(
                channel_id,
                format_type,
                enabled=enabled,
                owner_price_band=owner_price_band,
                platform_promo_enabled=platform_promo_enabled,
                custom_multiplier_bps=custom_multiplier_bps,
            )
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="set_format_policy_for_publisher",
                actor_telegram_user_id=publisher_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="set_format_policy_for_publisher",
            actor_telegram_user_id=publisher_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"{format_type} → {owner_price_band}, enabled={int(enabled)}",
        )
        return policy

    def set_daily_ad_limit_for_publisher(
        self,
        *,
        publisher_telegram_user_id: str | int,
        channel_id: str,
        daily_ad_limit: int,
        actor_kind: str = "human",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {"channel_id": channel_id, "daily_ad_limit": daily_ad_limit}
        try:
            with self.db.transaction() as conn:
                self._verify_publisher_owns_channel(
                    conn,
                    publisher_telegram_user_id=publisher_telegram_user_id,
                    channel_id=channel_id,
                )
            cfg = self.set_daily_ad_limit(channel_id, daily_ad_limit)
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="set_daily_ad_limit_for_publisher",
                actor_telegram_user_id=publisher_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="set_daily_ad_limit_for_publisher",
            actor_telegram_user_id=publisher_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"daily_ad_limit → {daily_ad_limit}",
        )
        return cfg

    def set_daily_ad_limit(self, channel_id: str, daily_ad_limit: int) -> dict[str, Any]:
        if daily_ad_limit < 1 or daily_ad_limit > 24:
            raise InvalidState("每日广告条数需要在 1 到 24 之间")
        with self.db.transaction() as conn:
            self.get_channel(conn, channel_id)
            conn.execute(
                """
                UPDATE channel_configs
                SET daily_ad_limit = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ?
                """,
                (daily_ad_limit, channel_id),
            )
            row = conn.execute(
                "SELECT * FROM channel_configs WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            return dict(row)

    def update_rate(self, channel_id: str, slot_type: str, unit_price_cents: int) -> dict[str, Any]:
        slot_type = self.normalize_slot_type(slot_type)
        with self.db.transaction() as conn:
            self._ensure_default_rate_cards(conn, channel_id)
            slot = conn.execute(
                "SELECT * FROM ad_slots WHERE channel_id = ? AND slot_type = ?",
                (channel_id, slot_type),
            ).fetchone()
            if not slot:
                raise NotFound(f"slot not found: {slot_type}")
            conn.execute("UPDATE rate_cards SET active = 0 WHERE slot_id = ?", (slot["id"],))
            conn.execute(
                """
                INSERT INTO rate_cards (id, slot_id, unit_price_cents, pricing_unit)
                VALUES (?, ?, ?, ?)
                """,
                (new_id("rate"), slot["id"], unit_price_cents, self.DEFAULT_RATES[slot_type][0]),
            )
            return self.get_channel(conn, channel_id)

    def get_by_token(self, conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM channels WHERE ref_token = ?", (token,)).fetchone()
        return row_to_dict(row)

    def get_by_chat_id(self, conn: sqlite3.Connection, telegram_chat_id: str | int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM channels WHERE telegram_chat_id = ?", (str(telegram_chat_id),)).fetchone()
        return row_to_dict(row)

    def get_channel(self, conn: sqlite3.Connection, channel_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not row:
            raise NotFound(f"channel not found: {channel_id}")
        return dict(row)

    def list_publisher_channels(
        self,
        *,
        publisher_telegram_user_id: str | int,
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            account = conn.execute(
                "SELECT id FROM accounts WHERE telegram_user_id = ?",
                (str(publisher_telegram_user_id),),
            ).fetchone()
            if not account:
                return []
            rows = conn.execute(
                """
                SELECT id, telegram_chat_id, title, username, ref_token, status,
                       created_at, updated_at
                FROM channels
                WHERE owner_account_id = ?
                ORDER BY updated_at DESC, created_at DESC
                """,
                (account["id"],),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_channel_view(
        self,
        channel_id: str,
        *,
        publisher_telegram_user_id: str | int | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            channel = conn.execute(
                "SELECT * FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()
            if not channel:
                raise NotFound(f"频道不存在：{channel_id}")
            channel_dict = dict(channel)
            if publisher_telegram_user_id is not None:
                account = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(publisher_telegram_user_id),),
                ).fetchone()
                if not account or channel_dict["owner_account_id"] != account["id"]:
                    raise NotFound(f"频道不存在：{channel_id}")
            self._ensure_default_rate_cards(conn, channel_id)
            self._ensure_default_format_policies(conn, channel_id)
            config = conn.execute(
                "SELECT * FROM channel_configs WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            policies = conn.execute(
                "SELECT format_type, enabled, owner_price_band, platform_promo_enabled, "
                "       custom_multiplier_bps "
                "FROM channel_ad_format_policies WHERE channel_id = ? ORDER BY format_type",
                (channel_id,),
            ).fetchall()
            rates = conn.execute(
                """
                SELECT s.slot_type, r.unit_price_cents, r.currency, r.pricing_unit
                FROM ad_slots s
                JOIN rate_cards r ON r.slot_id = s.id AND r.active = 1
                WHERE s.channel_id = ?
                ORDER BY s.slot_type
                """,
                (channel_id,),
            ).fetchall()
            today_ads = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE channel_id = ? "
                "AND status IN ('sent', 'confirmed') AND DATE(sent_at) = DATE('now')",
                (channel_id,),
            ).fetchone()["n"]
            pending = conn.execute(
                "SELECT COALESCE(SUM(publisher_net_cents - publisher_reversed_cents), 0) AS n "
                "FROM deliveries WHERE channel_id = ? AND status IN ('sent', 'confirmed')",
                (channel_id,),
            ).fetchone()["n"]
            return {
                **channel_dict,
                "config": dict(config) if config else None,
                "format_policies": [dict(row) for row in policies],
                "rate_cards": [dict(row) for row in rates],
                "today_ads": today_ads,
                "pending_earnings_cents": pending,
            }

    def get_rate(self, conn: sqlite3.Connection, channel_id: str, slot_type: str) -> dict[str, Any]:
        slot_type = self.normalize_slot_type(slot_type)
        self._ensure_default_rate_cards(conn, channel_id)
        self._ensure_default_format_policies(conn, channel_id)
        row = conn.execute(
            """
            SELECT r.*, s.slot_type, s.channel_id
            FROM rate_cards r
            JOIN ad_slots s ON s.id = r.slot_id
            WHERE s.channel_id = ? AND s.slot_type = ? AND s.enabled = 1 AND r.active = 1
            ORDER BY r.created_at DESC
            LIMIT 1
            """,
            (channel_id, slot_type),
        ).fetchone()
        if not row:
            raise NotFound(f"active rate not found for {slot_type}")
        return dict(row)

    def start_url(self, channel: dict[str, Any]) -> str:
        return f"https://t.me/{self.settings.bot_username}?start=ch_{channel['ref_token']}"

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor_account_id: str | None,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_logs (id, actor_account_id, action, entity_type, entity_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("aud"), actor_account_id, action, entity_type, entity_id, json.dumps(payload, ensure_ascii=False)),
        )


class LightProbeService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.channels = ChannelService(db, settings)

    def create_probe(
        self,
        *,
        channel_id: str,
        short_text: str,
        detail_text: str,
        target_url: str,
        button_text: str = "了解详情",
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not short_text.strip():
            raise InvalidState("轻插播探针必须有短文案")
        if not detail_text.strip():
            raise InvalidState("轻插播探针必须有详情文案")
        if not target_url.strip():
            raise InvalidState("轻插播探针必须有目标链接")
        with self.db.transaction() as conn:
            self.channels.get_channel(conn, channel_id)
            probe_id = new_id("lp")
            conn.execute(
                """
                INSERT INTO light_probes (
                    id, channel_id, short_text, detail_text, target_url,
                    button_text, start_at, end_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    probe_id,
                    channel_id,
                    short_text,
                    detail_text,
                    target_url,
                    button_text,
                    iso(start_at) if start_at else None,
                    iso(end_at) if end_at else None,
                ),
            )
            return dict(conn.execute("SELECT * FROM light_probes WHERE id = ?", (probe_id,)).fetchone())

    def pause_probe(self, probe_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM light_probes WHERE id = ?", (probe_id,)).fetchone()
            if not row:
                raise NotFound(f"light probe not found: {probe_id}")
            conn.execute(
                "UPDATE light_probes SET status = 'paused', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (probe_id,),
            )
            return dict(conn.execute("SELECT * FROM light_probes WHERE id = ?", (probe_id,)).fetchone())

    def get_active_for_channel(self, conn: sqlite3.Connection, channel_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT *
            FROM light_probes
            WHERE channel_id = ?
              AND status = 'active'
              AND (start_at IS NULL OR start_at <= ?)
              AND (end_at IS NULL OR end_at > ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (channel_id, iso(), iso()),
        ).fetchone()
        return row_to_dict(row)

    def start_url(self, probe: dict[str, Any]) -> str:
        return f"https://t.me/{self.settings.bot_username}?start=probe_{probe['id']}"

    def record_click(
        self,
        conn: sqlite3.Connection,
        *,
        probe_id: str,
        telegram_user_id: str | int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        probe = conn.execute("SELECT * FROM light_probes WHERE id = ?", (probe_id,)).fetchone()
        if not probe:
            raise NotFound(f"light probe not found: {probe_id}")
        event_id = new_id("lpe")
        conn.execute(
            """
            INSERT INTO light_probe_events (
                id, probe_id, channel_id, telegram_user_id, event_type, metadata_json
            )
            VALUES (?, ?, ?, ?, 'bot_start', ?)
            """,
            (
                event_id,
                probe_id,
                probe["channel_id"],
                str(telegram_user_id),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        return dict(conn.execute("SELECT * FROM light_probe_events WHERE id = ?", (event_id,)).fetchone())

    def stats(self, *, channel_id: str | None = None, probe_id: str | None = None) -> dict[str, Any]:
        if not channel_id and not probe_id:
            raise InvalidState("必须指定 channel_id 或 probe_id")
        where = []
        params: list[Any] = []
        if channel_id:
            where.append("channel_id = ?")
            params.append(channel_id)
        if probe_id:
            where.append("probe_id = ?")
            params.append(probe_id)
        where_sql = " AND ".join(where)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_clicks,
                    COUNT(DISTINCT telegram_user_id) AS unique_clickers
                FROM light_probe_events
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
            return {
                "channel_id": channel_id,
                "probe_id": probe_id,
                "total_clicks": row["total_clicks"],
                "unique_clickers": row["unique_clickers"],
            }


class SelfPromoService:
    """Channel owners' free self-publishing entry — same 3-button layout as
    paid placements, but no money moves and no advertiser/order is created.

    Each publish is recorded so the 查看详情 deep link (start=sp_<id>) can
    render the full ad page later, and so dashboards can audit the channel's
    self-promo history.
    """

    PUBLISHABLE_FORMATS = {"standard_card", "strong_post"}

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings)
        self.tool_calls = ToolCallLogService(db, settings)

    def list_publishable_materials(
        self,
        *,
        publisher_telegram_user_id: str | int,
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            account = conn.execute(
                "SELECT id FROM accounts WHERE telegram_user_id = ?",
                (str(publisher_telegram_user_id),),
            ).fetchone()
            if not account:
                return []
            rows = conn.execute(
                """
                SELECT * FROM creatives
                WHERE advertiser_account_id = ?
                  AND archived_at IS NULL
                  AND format_type IN ('standard_card', 'strong_post')
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 20
                """,
                (account["id"],),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_self_promo(self, self_promo_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM self_promo_publishes WHERE id = ?", (self_promo_id,)
            ).fetchone()
            if not row:
                raise NotFound(f"自用发布记录不存在：{self_promo_id}")
            return dict(row)

    def prepare_publish(
        self,
        *,
        publisher_telegram_user_id: str | int,
        channel_id: str,
        material_id: str,
        actor_kind: str = "human",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a pending self-promo row and return all data needed to send.

        The actual `gateway.send_ad` call happens outside the DB transaction;
        the caller writes the resulting message_id back via mark_sent /
        mark_failed.
        """
        audit_args = {"channel_id": channel_id, "material_id": material_id}
        try:
            with self.db.transaction() as conn:
                channel = self.channels.get_channel(conn, channel_id)
                publisher = conn.execute(
                    "SELECT * FROM accounts WHERE telegram_user_id = ?",
                    (str(publisher_telegram_user_id),),
                ).fetchone()
                if not publisher or publisher["id"] != channel["owner_account_id"]:
                    raise NotFound(f"频道不属于该用户：{channel_id}")
                material = conn.execute(
                    "SELECT * FROM creatives WHERE id = ?", (material_id,)
                ).fetchone()
                if not material or material["advertiser_account_id"] != publisher["id"]:
                    raise NotFound(f"广告素材不存在：{material_id}")
                if material["archived_at"]:
                    raise InvalidState("广告素材已归档，无法用于自用发布")
                if material["format_type"] not in self.PUBLISHABLE_FORMATS:
                    raise InvalidState("自用发布暂只支持标准插播或定制插播素材")
                self_promo_id = new_id("sp")
                conn.execute(
                    """
                    INSERT INTO self_promo_publishes (
                        id, channel_id, creative_id, publisher_account_id, status
                    )
                    VALUES (?, ?, ?, ?, 'pending')
                    """,
                    (self_promo_id, channel["id"], material["id"], publisher["id"]),
                )
                prepared = {
                    "self_promo_id": self_promo_id,
                    "channel": dict(channel),
                    "material": dict(material),
                }
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="self_promo_prepare_publish",
                actor_telegram_user_id=publisher_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="self_promo_prepare_publish",
            actor_telegram_user_id=publisher_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"prepared {prepared['self_promo_id']}",
        )
        return prepared

    def mark_sent(self, self_promo_id: str, *, message_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE self_promo_publishes
                SET status = 'sent', message_id = ?, sent_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message_id, iso(), self_promo_id),
            )
            row = conn.execute(
                "SELECT * FROM self_promo_publishes WHERE id = ?", (self_promo_id,)
            ).fetchone()
            return dict(row)

    def mark_failed(self, self_promo_id: str, *, error: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE self_promo_publishes
                SET status = 'failed', error_message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error, self_promo_id),
            )
            row = conn.execute(
                "SELECT * FROM self_promo_publishes WHERE id = ?", (self_promo_id,)
            ).fetchone()
            return dict(row)


class PricingService:
    CATEGORY_CPM_CENTS = {
        "finance": 600,
        "web3": 600,
        "ai": 500,
        "software": 450,
        "education": 350,
        "recruiting": 350,
        "ecommerce": 300,
        "tools": 300,
        "vertical": 350,
        "news": 200,
        "gossip": 130,
        "funny": 120,
        "entertainment": 120,
        "media_resource": 80,
        "adult_restricted": 60,
        "general": 180,
    }
    FORMAT_FACTORS_BPS = {
        "light_tail": 3000,
        "button_tail": 5000,
        "standard_card": 10000,
        "strong_post": 18000,
        "pin24h": 25000,
        "loop_daily": 8000,
    }
    BAND_FACTORS_BPS = {
        "low": 8500,
        "medium": 10000,
        "high": 12500,
    }
    RISK_FACTORS_BPS = {
        "normal": 10000,
        "watch": 7500,
        "high": 4500,
        "blocked": 0,
    }

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.channels = ChannelService(db, settings)

    def assess_channel(
        self,
        *,
        channel_id: str,
        category: str = "general",
        median_24h_views: int = 0,
        subscribers: int = 0,
        light_clicks_30d: int = 0,
        light_unique_clickers_30d: int = 0,
        repeat_purchase_count: int = 0,
        dispute_count: int = 0,
        risk_level: str = "normal",
    ) -> dict[str, Any]:
        category_cpm = self.CATEGORY_CPM_CENTS.get(category, self.CATEGORY_CPM_CENTS["general"])
        estimated_reach = median_24h_views or max(int(subscribers * 0.08), 100)
        reach_units = max(estimated_reach / 1000, 0.1)
        interaction_bps = self._interaction_bps(light_unique_clickers_30d, estimated_reach)
        history_bps = self._history_bps(repeat_purchase_count, dispute_count)
        risk_bps = self.RISK_FACTORS_BPS.get(risk_level, self.RISK_FACTORS_BPS["watch"])
        base = int(reach_units * category_cpm)
        base = base * interaction_bps // 10_000
        base = base * history_bps // 10_000
        base = base * risk_bps // 10_000
        if risk_bps == 0:
            base = 0
        else:
            base = max(base, 100)
        score = self._score(interaction_bps, history_bps, risk_bps)
        breakdown = {
            "category_cpm_cents": category_cpm,
            "estimated_reach": estimated_reach,
            "interaction_bps": interaction_bps,
            "history_bps": history_bps,
            "risk_bps": risk_bps,
            "pricing_note": "浏览量只用于估价，不作为一期结算依据",
        }
        with self.db.transaction() as conn:
            self.channels.get_channel(conn, channel_id)
            assessment_id = new_id("price")
            conn.execute(
                """
                INSERT INTO channel_pricing_assessments (
                    id, channel_id, category, median_24h_views, subscribers,
                    light_clicks_30d, light_unique_clickers_30d, repeat_purchase_count,
                    dispute_count, risk_level, base_standard_price_cents, score, breakdown_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_id,
                    channel_id,
                    category,
                    median_24h_views,
                    subscribers,
                    light_clicks_30d,
                    light_unique_clickers_30d,
                    repeat_purchase_count,
                    dispute_count,
                    risk_level,
                    base,
                    score,
                    json.dumps(breakdown, ensure_ascii=False),
                ),
            )
            return dict(conn.execute("SELECT * FROM channel_pricing_assessments WHERE id = ?", (assessment_id,)).fetchone())

    def quote_channel(self, channel_id: str, slot_type: str, owner_price_band: str | None = None) -> dict[str, Any]:
        slot_type = self.channels.normalize_slot_type(slot_type)
        with self.db.transaction() as conn:
            latest = self._latest_assessment(conn, channel_id)
            if not latest:
                raise InvalidState("频道还没有定价评估，不能报价")
            self.channels._ensure_default_format_policies(conn, channel_id)
            policy = conn.execute(
                "SELECT * FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = ?",
                (channel_id, slot_type),
            ).fetchone()
            if not policy:
                raise NotFound(f"unknown ad format: {slot_type}")
            if not policy["enabled"]:
                raise InvalidState("该频道主当前未开启这种插播广告形态")
            band = owner_price_band or policy["owner_price_band"]
            band_bps = policy["custom_multiplier_bps"] if band == "custom" else self.BAND_FACTORS_BPS.get(band, 10000)
            format_bps = self.FORMAT_FACTORS_BPS[slot_type]
            list_price = latest["base_standard_price_cents"] * format_bps // 10_000
            list_price = list_price * band_bps // 10_000
            return {
                "channel_id": channel_id,
                "slot_type": slot_type,
                "category": latest["category"],
                "score": latest["score"],
                "risk_level": latest["risk_level"],
                "base_standard_price_cents": latest["base_standard_price_cents"],
                "owner_price_band": band,
                "format_factor_bps": format_bps,
                "band_factor_bps": band_bps,
                "list_price_cents": list_price,
                "platform_promo_enabled": bool(policy["platform_promo_enabled"]),
            }

    def apply_quotes_to_rate_cards(self, channel_id: str) -> list[dict[str, Any]]:
        quotes: list[dict[str, Any]] = []
        with self.db.transaction() as conn:
            self.channels._ensure_default_format_policies(conn, channel_id)
            rows = conn.execute(
                "SELECT * FROM channel_ad_format_policies WHERE channel_id = ? AND enabled = 1",
                (channel_id,),
            ).fetchall()
        for row in rows:
            quote = self.quote_channel(channel_id, row["format_type"])
            self.channels.update_rate(channel_id, row["format_type"], quote["list_price_cents"])
            quotes.append(quote)
        return quotes

    def _latest_assessment(self, conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM channel_pricing_assessments
            WHERE channel_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (channel_id,),
        ).fetchone()

    def _interaction_bps(self, unique_clickers: int, estimated_reach: int) -> int:
        if unique_clickers <= 0:
            return 6000
        rate = unique_clickers / max(estimated_reach, 1)
        if rate >= 0.01:
            return 14000
        if rate >= 0.005:
            return 12000
        if rate >= 0.002:
            return 10000
        if rate >= 0.0005:
            return 8000
        return 6500

    def _history_bps(self, repeat_purchase_count: int, dispute_count: int) -> int:
        bps = 10000 + min(repeat_purchase_count, 5) * 500 - min(dispute_count, 5) * 1200
        return max(5000, min(13000, bps))

    def _score(self, interaction_bps: int, history_bps: int, risk_bps: int) -> int:
        score = int((interaction_bps * 0.4 + history_bps * 0.3 + risk_bps * 0.3) / 100)
        return max(0, min(100, score))
