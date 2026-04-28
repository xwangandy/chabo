from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .db import Database
from .ids import new_id, new_ref_token
from .money import bps_amount


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ChaboError(RuntimeError):
    pass


class NotFound(ChaboError):
    pass


class InsufficientBalance(ChaboError):
    pass


class InvalidState(ChaboError):
    pass


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class AccountService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def ensure_platform_account(self, conn: sqlite3.Connection) -> str:
        account_id = self.settings.platform_account_id
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts (id, role, display_name)
            VALUES (?, 'platform', '插播平台')
            """,
            (account_id,),
        )
        return account_id

    def get_or_create_by_telegram(
        self,
        conn: sqlite3.Connection,
        telegram_user_id: str | int,
        role: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        telegram_user_id = str(telegram_user_id)
        row = conn.execute(
            "SELECT * FROM accounts WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if row:
            account = dict(row)
            next_role = self._merge_role(account["role"], role)
            if next_role != account["role"] or (display_name and display_name != account["display_name"]):
                conn.execute(
                    """
                    UPDATE accounts
                    SET role = ?, display_name = COALESCE(?, display_name), updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (next_role, display_name, account["id"]),
                )
                account = dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (account["id"],)).fetchone())
            return account
        account_id = new_id("acct")
        conn.execute(
            """
            INSERT INTO accounts (id, telegram_user_id, role, display_name)
            VALUES (?, ?, ?, ?)
            """,
            (account_id, telegram_user_id, role, display_name),
        )
        return dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())

    def _merge_role(self, current_role: str, requested_role: str) -> str:
        if current_role == requested_role:
            return current_role
        if current_role == "platform" or requested_role == "platform":
            return current_role
        if current_role == "mixed" or requested_role == "mixed":
            return "mixed"
        return "mixed"

    def get(self, conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise NotFound(f"account not found: {account_id}")
        return dict(row)


class LedgerService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)

    def _record(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        tx_type: str,
        amount_cents: int,
        *,
        related_account_id: str | None = None,
        order_id: str | None = None,
        delivery_id: str | None = None,
        currency: str = "USD",
        memo: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        account = self.accounts.get(conn, account_id)
        conn.execute(
            """
            INSERT INTO ledger_transactions (
                id, account_id, related_account_id, order_id, delivery_id, type,
                currency, amount_cents, available_after_cents, reserved_after_cents,
                spent_after_cents, pending_after_cents, confirmed_after_cents,
                releasable_after_cents, memo, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("led"),
                account_id,
                related_account_id,
                order_id,
                delivery_id,
                tx_type,
                currency,
                amount_cents,
                account["available_balance_cents"],
                account["reserved_balance_cents"],
                account["spent_balance_cents"],
                account["pending_earnings_cents"],
                account["confirmed_earnings_cents"],
                account["releasable_earnings_cents"],
                memo,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )

    def manual_topup(
        self,
        telegram_user_id: str | int,
        amount_cents: int,
        *,
        display_name: str | None = None,
        memo: str = "人工入账",
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, telegram_user_id, "advertiser", display_name)
            conn.execute(
                """
                UPDATE accounts
                SET available_balance_cents = available_balance_cents + ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (amount_cents, account["id"]),
            )
            self._record(conn, account["id"], "manual_topup", amount_cents, memo=memo)
            return self.accounts.get(conn, account["id"])

    def stars_topup(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        amount_cents: int,
        telegram_charge_id: str,
    ) -> None:
        self.accounts.get(conn, account_id)
        conn.execute(
            """
            UPDATE accounts
            SET available_balance_cents = available_balance_cents + ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, account_id),
        )
        self._record(
            conn,
            account_id,
            "stars_topup",
            amount_cents,
            memo="Telegram Stars 入账",
            metadata={"telegram_charge_id": telegram_charge_id},
        )

    def reserve_budget(
        self,
        conn: sqlite3.Connection,
        advertiser_account_id: str,
        order_id: str,
        amount_cents: int,
        currency: str = "USD",
    ) -> None:
        account = self.accounts.get(conn, advertiser_account_id)
        if account["available_balance_cents"] < amount_cents:
            raise InsufficientBalance("插播余额不足，无法冻结预算")
        conn.execute(
            """
            UPDATE accounts
            SET available_balance_cents = available_balance_cents - ?,
                reserved_balance_cents = reserved_balance_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, amount_cents, advertiser_account_id),
        )
        self._record(conn, advertiser_account_id, "budget_reserved", amount_cents, order_id=order_id, currency=currency)

    def release_reserved(
        self,
        conn: sqlite3.Connection,
        advertiser_account_id: str,
        order_id: str,
        amount_cents: int,
        *,
        delivery_id: str | None = None,
        reason: str = "释放未使用插播预算",
        currency: str = "USD",
    ) -> None:
        account = self.accounts.get(conn, advertiser_account_id)
        amount_cents = min(amount_cents, account["reserved_balance_cents"])
        if amount_cents <= 0:
            return
        conn.execute(
            """
            UPDATE accounts
            SET reserved_balance_cents = reserved_balance_cents - ?,
                available_balance_cents = available_balance_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, amount_cents, advertiser_account_id),
        )
        self._record(
            conn,
            advertiser_account_id,
            "budget_released",
            amount_cents,
            order_id=order_id,
            delivery_id=delivery_id,
            currency=currency,
            memo=reason,
        )

    def charge_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        advertiser_account_id: str,
        publisher_account_id: str,
        order_id: str,
        delivery_id: str,
        gross_cents: int,
        service_fee_bps: int,
        currency: str = "USD",
    ) -> tuple[int, int]:
        advertiser = self.accounts.get(conn, advertiser_account_id)
        if advertiser["reserved_balance_cents"] < gross_cents:
            raise InsufficientBalance("冻结插播预算不足，无法扣费")
        platform_account_id = self.accounts.ensure_platform_account(conn)
        fee_cents = bps_amount(gross_cents, service_fee_bps)
        publisher_net_cents = gross_cents - fee_cents

        conn.execute(
            """
            UPDATE accounts
            SET reserved_balance_cents = reserved_balance_cents - ?,
                spent_balance_cents = spent_balance_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (gross_cents, gross_cents, advertiser_account_id),
        )
        self._record(
            conn,
            advertiser_account_id,
            "delivery_charged",
            gross_cents,
            related_account_id=publisher_account_id,
            order_id=order_id,
            delivery_id=delivery_id,
            currency=currency,
            memo="插播发布成功扣费",
        )

        conn.execute(
            """
            UPDATE accounts
            SET pending_earnings_cents = pending_earnings_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (publisher_net_cents, publisher_account_id),
        )
        self._record(
            conn,
            publisher_account_id,
            "publisher_pending_earning",
            publisher_net_cents,
            related_account_id=advertiser_account_id,
            order_id=order_id,
            delivery_id=delivery_id,
            currency=currency,
            memo="插播收益进入观察期",
            metadata={"service_fee_cents": fee_cents},
        )

        if fee_cents:
            conn.execute(
                """
                UPDATE accounts
                SET available_balance_cents = available_balance_cents + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (fee_cents, platform_account_id),
            )
            self._record(
                conn,
                platform_account_id,
                "platform_service_fee",
                fee_cents,
                related_account_id=publisher_account_id,
                order_id=order_id,
                delivery_id=delivery_id,
                currency=currency,
                memo="插播平台服务费",
            )

        return publisher_net_cents, fee_cents

    def refund_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        advertiser_account_id: str,
        publisher_account_id: str,
        order_id: str,
        delivery_id: str,
        gross_cents: int,
        publisher_net_cents: int,
        platform_fee_cents: int,
        reason: str,
        currency: str = "USD",
    ) -> None:
        advertiser = self.accounts.get(conn, advertiser_account_id)
        if advertiser["spent_balance_cents"] < gross_cents:
            raise InvalidState("广告主已花费余额不足，无法退款")
        publisher = self.accounts.get(conn, publisher_account_id)
        if publisher["pending_earnings_cents"] < publisher_net_cents:
            raise InvalidState("频道主待确认收益不足，无法自动退款；需要人工裁决")
        platform_account_id = self.accounts.ensure_platform_account(conn)
        platform = self.accounts.get(conn, platform_account_id)
        if platform["available_balance_cents"] < platform_fee_cents:
            raise InvalidState("平台服务费余额不足，无法自动退款；需要人工裁决")

        conn.execute(
            """
            UPDATE accounts
            SET available_balance_cents = available_balance_cents + ?,
                spent_balance_cents = spent_balance_cents - ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (gross_cents, gross_cents, advertiser_account_id),
        )
        self._record(
            conn,
            advertiser_account_id,
            "delivery_refunded",
            gross_cents,
            related_account_id=publisher_account_id,
            order_id=order_id,
            delivery_id=delivery_id,
            currency=currency,
            memo=reason,
        )

        if publisher_net_cents:
            conn.execute(
                """
                UPDATE accounts
                SET pending_earnings_cents = pending_earnings_cents - ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (publisher_net_cents, publisher_account_id),
            )
            self._record(
                conn,
                publisher_account_id,
                "publisher_earning_reversed",
                -publisher_net_cents,
                related_account_id=advertiser_account_id,
                order_id=order_id,
                delivery_id=delivery_id,
                currency=currency,
                memo=reason,
            )

        if platform_fee_cents:
            conn.execute(
                """
                UPDATE accounts
                SET available_balance_cents = available_balance_cents - ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (platform_fee_cents, platform_account_id),
            )
            self._record(
                conn,
                platform_account_id,
                "platform_fee_reversed",
                -platform_fee_cents,
                related_account_id=publisher_account_id,
                order_id=order_id,
                delivery_id=delivery_id,
                currency=currency,
                memo=reason,
            )

    def charge_subscription(
        self,
        conn: sqlite3.Connection,
        *,
        payer_account_id: str,
        channel_id: str,
        amount_cents: int,
        months: int,
        subscriber_count: int,
        currency: str = "USD",
    ) -> None:
        payer = self.accounts.get(conn, payer_account_id)
        if payer["available_balance_cents"] < amount_cents:
            raise InsufficientBalance("频道主余额不足，无法开通频道高级订阅")
        platform_account_id = self.accounts.ensure_platform_account(conn)
        conn.execute(
            """
            UPDATE accounts
            SET available_balance_cents = available_balance_cents - ?,
                spent_balance_cents = spent_balance_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, amount_cents, payer_account_id),
        )
        self._record(
            conn,
            payer_account_id,
            "publisher_subscription_charged",
            amount_cents,
            currency=currency,
            memo="频道高级订阅扣费",
            metadata={"channel_id": channel_id, "months": months, "subscriber_count": subscriber_count},
        )
        conn.execute(
            """
            UPDATE accounts
            SET available_balance_cents = available_balance_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, platform_account_id),
        )
        self._record(
            conn,
            platform_account_id,
            "publisher_subscription_revenue",
            amount_cents,
            related_account_id=payer_account_id,
            currency=currency,
            memo="频道高级订阅收入",
            metadata={"channel_id": channel_id, "months": months, "subscriber_count": subscriber_count},
        )

    def charge_advertiser_subscription(
        self,
        conn: sqlite3.Connection,
        *,
        advertiser_account_id: str,
        plan: str,
        amount_cents: int,
        months: int,
        currency: str = "USD",
    ) -> None:
        advertiser = self.accounts.get(conn, advertiser_account_id)
        if advertiser["available_balance_cents"] < amount_cents:
            raise InsufficientBalance("广告主余额不足，无法开通高级服务")
        platform_account_id = self.accounts.ensure_platform_account(conn)
        conn.execute(
            """
            UPDATE accounts
            SET available_balance_cents = available_balance_cents - ?,
                spent_balance_cents = spent_balance_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, amount_cents, advertiser_account_id),
        )
        self._record(
            conn,
            advertiser_account_id,
            "advertiser_subscription_charged",
            amount_cents,
            currency=currency,
            memo="广告主高级服务扣费",
            metadata={"plan": plan, "months": months},
        )
        conn.execute(
            """
            UPDATE accounts
            SET available_balance_cents = available_balance_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, platform_account_id),
        )
        self._record(
            conn,
            platform_account_id,
            "advertiser_subscription_revenue",
            amount_cents,
            related_account_id=advertiser_account_id,
            currency=currency,
            memo="广告主高级服务收入",
            metadata={"plan": plan, "months": months},
        )

    def confirm_publisher_earning(self, conn: sqlite3.Connection, delivery_id: str) -> None:
        delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if not delivery:
            raise NotFound(f"delivery not found: {delivery_id}")
        channel = conn.execute("SELECT * FROM channels WHERE id = ?", (delivery["channel_id"],)).fetchone()
        config = conn.execute("SELECT * FROM channel_configs WHERE channel_id = ?", (delivery["channel_id"],)).fetchone()
        if not channel or not config:
            raise NotFound("channel/config not found")
        publisher_account_id = channel["owner_account_id"]
        net_cents = delivery["publisher_net_cents"] - delivery["publisher_reversed_cents"]
        if net_cents <= 0:
            return
        publisher = self.accounts.get(conn, publisher_account_id)
        if publisher["pending_earnings_cents"] < net_cents:
            raise InvalidState("频道主待确认收益不足")
        releasable = net_cents - bps_amount(net_cents, config["holdback_bps"])
        conn.execute(
            """
            UPDATE accounts
            SET pending_earnings_cents = pending_earnings_cents - ?,
                confirmed_earnings_cents = confirmed_earnings_cents + ?,
                releasable_earnings_cents = releasable_earnings_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (net_cents, net_cents, releasable, publisher_account_id),
        )
        self._record(
            conn,
            publisher_account_id,
            "publisher_earning_confirmed",
            net_cents,
            order_id=delivery["order_id"],
            delivery_id=delivery_id,
            memo="插播观察期通过，收益确认",
            metadata={"releasable_cents": releasable, "holdback_bps": config["holdback_bps"]},
        )


class ChannelService:
    SLOT_ALIASES = {
        "standard": "standard_card",
        "scheduled": "standard_card",
    }
    DEFAULT_RATES = {
        "light_tail": ("per_tail", 300),
        "standard_card": ("per_post", 1_000),
        "strong_post": ("per_post", 1_800),
        "pin24h": ("per_24h_pin", 2_500),
        "loop_daily": ("per_day", 800),
    }

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)

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
            self.get_channel(conn, channel_id)
            has_premium = SubscriptionService.has_active_subscription(conn, channel_id)
            wants_advanced = (not platform_promo_enabled) or owner_price_band == "custom" or custom_multiplier_bps is not None
            if wants_advanced and not has_premium:
                raise InvalidState("该配置属于频道高级功能，需要先开通频道高级订阅")
            self._ensure_default_format_policies(conn, channel_id)
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


class OrderService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings)
        self.ledger = LedgerService(db, settings)

    def create_order(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        channel_token: str,
        slot_type: str,
        text: str,
        target_url: str,
        budget_cents: int,
        button_text: str = "查看详情",
        category: str = "general",
        scheduled_at: datetime | None = None,
        end_at: datetime | None = None,
        frequency_per_day: int = 1,
        campaign_name: str = "插播广告",
        unit_price_override_cents: int | None = None,
        price_offer_id: str | None = None,
    ) -> dict[str, Any]:
        scheduled_at = scheduled_at or utcnow()
        slot_type = self.channels.normalize_slot_type(slot_type)
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            channel = self.channels.get_by_token(conn, channel_token)
            if not channel:
                raise NotFound("频道插播入口不存在或已失效")
            rate = self.channels.get_rate(conn, channel["id"], slot_type)
            policy = conn.execute(
                """
                SELECT * FROM channel_ad_format_policies
                WHERE channel_id = ? AND format_type = ?
                """,
                (channel["id"], slot_type),
            ).fetchone()
            if policy and not policy["enabled"]:
                raise InvalidState("该频道主当前未开启这种插播广告形态")
            effective_service_fee_bps = 0 if SubscriptionService.has_active_subscription(conn, channel["id"]) else self.settings.default_service_fee_bps
            if policy and policy["platform_promo_enabled"]:
                effective_service_fee_bps = 0
            conn.execute(
                """
                UPDATE channel_configs
                SET service_fee_bps = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ?
                """,
                (effective_service_fee_bps, channel["id"]),
            )
            unit_price_cents = unit_price_override_cents or rate["unit_price_cents"]
            if budget_cents < unit_price_cents:
                raise InvalidState("插播预算低于该广告位单次刊例价")
            campaign_id = new_id("camp")
            creative_id = new_id("cre")
            order_id = new_id("ord")
            content_hash = hashlib.sha256(f"{text}|{target_url}|{button_text}".encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT INTO campaigns (id, advertiser_account_id, name)
                VALUES (?, ?, ?)
                """,
                (campaign_id, advertiser["id"], campaign_name),
            )
            conn.execute(
                """
                INSERT INTO creatives (
                    id, campaign_id, text, target_url, button_text, category, content_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (creative_id, campaign_id, text, target_url, button_text, category, content_hash),
            )
            conn.execute(
                """
                INSERT INTO ad_orders (
                    id, campaign_id, creative_id, advertiser_account_id, channel_id, slot_id,
                    budget_cents, reserved_cents, unit_price_cents, start_at, end_at,
                    scheduled_at, frequency_per_day, price_offer_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    campaign_id,
                    creative_id,
                    advertiser["id"],
                    channel["id"],
                    rate["slot_id"],
                    budget_cents,
                    budget_cents,
                    unit_price_cents,
                    iso(scheduled_at),
                    iso(end_at) if end_at else None,
                    iso(scheduled_at),
                    frequency_per_day,
                    price_offer_id,
                ),
            )
            self.ledger.reserve_budget(conn, advertiser["id"], order_id, budget_cents, rate["currency"])
            self._snapshot(conn, order_id, None, "creative", {"text": text, "target_url": target_url, "button_text": button_text})
            self._snapshot(conn, order_id, None, "rate_card", {**rate, "accepted_unit_price_cents": unit_price_cents, "price_offer_id": price_offer_id})
            self._snapshot(conn, order_id, None, "channel", channel)
            return self.get_order(conn, order_id)

    def approve_order(self, order_id: str, actor_account_id: str | None = None) -> dict[str, Any]:
        with self.db.transaction() as conn:
            order = self.get_order(conn, order_id)
            if order["status"] not in {"pending_review", "paused"}:
                raise InvalidState(f"订单状态不能审核通过: {order['status']}")
            conn.execute("UPDATE creatives SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order["creative_id"],))
            conn.execute(
                """
                UPDATE ad_orders
                SET status = 'approved', approved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order_id,),
            )
            self._ensure_delivery(conn, order_id, order["scheduled_at"])
            self._audit(conn, actor_account_id, "order_approved", "ad_order", order_id, {})
            return self.get_order(conn, order_id)

    def reject_order(self, order_id: str, reason: str, actor_account_id: str | None = None) -> dict[str, Any]:
        with self.db.transaction() as conn:
            order = self.get_order(conn, order_id)
            if order["spent_cents"] > 0:
                raise InvalidState("已有成功扣费的订单不能直接拒绝，请走退款或争议裁决")
            if order["status"] not in {"pending_review", "approved", "paused"}:
                raise InvalidState(f"订单状态不能拒绝: {order['status']}")
            if order["reserved_cents"]:
                self.ledger.release_reserved(
                    conn,
                    order["advertiser_account_id"],
                    order_id,
                    order["reserved_cents"],
                    reason=f"插播订单被拒绝：{reason}",
                    currency=order["currency"],
                )
            conn.execute("UPDATE creatives SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order["creative_id"],))
            conn.execute("UPDATE deliveries SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE order_id = ? AND status = 'scheduled'", (order_id,))
            conn.execute(
                """
                UPDATE ad_orders
                SET status = 'rejected', reserved_cents = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order_id,),
            )
            self._snapshot(conn, order_id, None, "order_rejected", {"reason": reason})
            self._audit(conn, actor_account_id, "order_rejected", "ad_order", order_id, {"reason": reason})
            return self.get_order(conn, order_id)

    def refund_delivery(self, delivery_id: str, reason: str, actor_account_id: str | None = None) -> dict[str, Any]:
        with self.db.transaction() as conn:
            delivery = self._refundable_delivery(conn, delivery_id)
            refundable_cents = delivery["charge_cents"] - delivery["refunded_cents"]
            return self._refund_delivery_locked(conn, delivery, reason, refundable_cents, actor_account_id)

    def refund_delivery_partial(
        self,
        delivery_id: str,
        amount_cents: int,
        reason: str,
        actor_account_id: str | None = None,
    ) -> dict[str, Any]:
        if amount_cents <= 0:
            raise InvalidState("退款金额必须大于 0")
        with self.db.transaction() as conn:
            delivery = self._refundable_delivery(conn, delivery_id)
            refundable_cents = delivery["charge_cents"] - delivery["refunded_cents"]
            if amount_cents > refundable_cents:
                raise InvalidState("退款金额超过该投放可退款余额")
            return self._refund_delivery_locked(conn, delivery, reason, amount_cents, actor_account_id)

    def _refundable_delivery(self, conn: sqlite3.Connection, delivery_id: str) -> sqlite3.Row:
        delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if not delivery:
            raise NotFound(f"delivery not found: {delivery_id}")
        if delivery["status"] not in {"sent", "disputed"}:
            raise InvalidState(f"当前投放状态不能退款: {delivery['status']}")
        if delivery["charge_cents"] <= 0:
            raise InvalidState("该投放没有成功扣费，不能退款")
        if delivery["charge_cents"] - delivery["refunded_cents"] <= 0:
            raise InvalidState("该投放已无可退款金额")
        return delivery

    def _refund_delivery_locked(
        self,
        conn: sqlite3.Connection,
        delivery: sqlite3.Row,
        reason: str,
        amount_cents: int,
        actor_account_id: str | None,
    ) -> dict[str, Any]:
        order = self.get_order(conn, delivery["order_id"])
        channel = conn.execute("SELECT * FROM channels WHERE id = ?", (delivery["channel_id"],)).fetchone()
        if not channel:
            raise NotFound("channel not found")
        refundable_cents = delivery["charge_cents"] - delivery["refunded_cents"]
        if amount_cents <= 0 or amount_cents > refundable_cents:
            raise InvalidState("退款金额不合法")
        publisher_net_cents, platform_fee_cents = self._split_refund_amount(delivery, amount_cents, refundable_cents)
        full_refund = amount_cents == refundable_cents
        self.ledger.refund_delivery(
            conn,
            advertiser_account_id=order["advertiser_account_id"],
            publisher_account_id=channel["owner_account_id"],
            order_id=order["id"],
            delivery_id=delivery["id"],
            gross_cents=amount_cents,
            publisher_net_cents=publisher_net_cents,
            platform_fee_cents=platform_fee_cents,
            reason=reason,
            currency=order["currency"],
        )
        if full_refund and order["reserved_cents"]:
            self.ledger.release_reserved(
                conn,
                order["advertiser_account_id"],
                order["id"],
                order["reserved_cents"],
                reason="插播退款后停止订单，释放剩余预算",
                currency=order["currency"],
            )
        order_status_sql = "status = 'refunded', reserved_cents = 0," if full_refund else ""
        conn.execute(
            f"""
            UPDATE ad_orders
            SET {order_status_sql}
                spent_cents = MAX(spent_cents - ?, 0),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, order["id"]),
        )
        delivery_status_sql = "status = 'refunded'," if full_refund else ""
        conn.execute(
            f"""
            UPDATE deliveries
            SET {delivery_status_sql}
                refunded_cents = refunded_cents + ?,
                publisher_reversed_cents = publisher_reversed_cents + ?,
                platform_fee_reversed_cents = platform_fee_reversed_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, publisher_net_cents, platform_fee_cents, delivery["id"]),
        )
        if full_refund:
            conn.execute(
                """
                UPDATE disputes
                SET status = 'resolved',
                    resolution = ?,
                    resolved_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ? AND status = 'open'
                """,
                (f"已退款：{reason}", delivery["id"]),
            )
        snapshot_type = "delivery_refunded" if full_refund else "delivery_partially_refunded"
        self._snapshot(
            conn,
            order["id"],
            delivery["id"],
            snapshot_type,
            {
                "reason": reason,
                "refund_cents": amount_cents,
                "publisher_net_reversed_cents": publisher_net_cents,
                "platform_fee_reversed_cents": platform_fee_cents,
                "full_refund": full_refund,
            },
        )
        self._audit(
            conn,
            actor_account_id,
            "delivery_refunded" if full_refund else "delivery_partially_refunded",
            "delivery",
            delivery["id"],
            {"reason": reason, "refund_cents": amount_cents, "full_refund": full_refund},
        )
        return {
            "order": self.get_order(conn, order["id"]),
            "delivery": dict(conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery["id"],)).fetchone()),
        }

    def _split_refund_amount(
        self,
        delivery: sqlite3.Row,
        amount_cents: int,
        refundable_cents: int,
    ) -> tuple[int, int]:
        remaining_net = delivery["publisher_net_cents"] - delivery["publisher_reversed_cents"]
        remaining_fee = delivery["platform_fee_cents"] - delivery["platform_fee_reversed_cents"]
        if amount_cents == refundable_cents:
            return remaining_net, remaining_fee
        publisher_net_cents = min(remaining_net, (amount_cents * remaining_net) // refundable_cents)
        platform_fee_cents = amount_cents - publisher_net_cents
        if platform_fee_cents > remaining_fee:
            platform_fee_cents = remaining_fee
            publisher_net_cents = amount_cents - platform_fee_cents
        if publisher_net_cents > remaining_net:
            publisher_net_cents = remaining_net
            platform_fee_cents = amount_cents - publisher_net_cents
        return publisher_net_cents, platform_fee_cents

    def pause_and_release(self, conn: sqlite3.Connection, order_id: str, reason: str) -> None:
        order = self.get_order(conn, order_id)
        remaining = order["reserved_cents"]
        if remaining:
            self.ledger.release_reserved(
                conn,
                order["advertiser_account_id"],
                order_id,
                remaining,
                reason=reason,
                currency=order["currency"],
            )
        conn.execute(
            """
            UPDATE ad_orders
            SET status = 'paused', reserved_cents = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (order_id,),
        )

    def get_order(self, conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            raise NotFound(f"order not found: {order_id}")
        return dict(row)

    def _ensure_delivery(self, conn: sqlite3.Connection, order_id: str, scheduled_at: str) -> str:
        existing = conn.execute(
            "SELECT id FROM deliveries WHERE order_id = ? AND status = 'scheduled' ORDER BY scheduled_at LIMIT 1",
            (order_id,),
        ).fetchone()
        if existing:
            return existing["id"]
        order = self.get_order(conn, order_id)
        delivery_id = new_id("del")
        conn.execute(
            """
            INSERT INTO deliveries (id, order_id, channel_id, creative_id, scheduled_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (delivery_id, order_id, order["channel_id"], order["creative_id"], scheduled_at),
        )
        return delivery_id

    def maybe_schedule_next(self, conn: sqlite3.Connection, order_id: str) -> None:
        order = self.get_order(conn, order_id)
        slot = conn.execute("SELECT * FROM ad_slots WHERE id = ?", (order["slot_id"],)).fetchone()
        remaining = order["reserved_cents"]
        if remaining < order["unit_price_cents"]:
            if remaining > 0:
                self.ledger.release_reserved(
                    conn,
                    order["advertiser_account_id"],
                    order_id,
                    remaining,
                    reason="插播预算不足以继续下一次发布，释放余款",
                    currency=order["currency"],
                )
            conn.execute(
                """
                UPDATE ad_orders
                SET status = 'budget_exhausted', reserved_cents = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order_id,),
            )
            return
        if slot["slot_type"] != "loop_daily":
            return
        next_at = parse_iso(order["scheduled_at"]) + timedelta(days=1)
        if order["end_at"] and next_at > parse_iso(order["end_at"]):
            self.ledger.release_reserved(
                conn,
                order["advertiser_account_id"],
                order_id,
                remaining,
                reason="插播周期结束，释放剩余预算",
                currency=order["currency"],
            )
            conn.execute(
                "UPDATE ad_orders SET status = 'done', reserved_cents = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (order_id,),
            )
            return
        conn.execute(
            "UPDATE ad_orders SET status = 'running', scheduled_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (iso(next_at), order_id),
        )
        self._ensure_delivery(conn, order_id, iso(next_at))

    def mark_low_budget_notified(self, conn: sqlite3.Connection, order_id: str) -> None:
        conn.execute(
            "UPDATE ad_orders SET low_budget_notified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (order_id,),
        )

    def _snapshot(
        self,
        conn: sqlite3.Connection,
        order_id: str | None,
        delivery_id: str | None,
        snapshot_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO evidence_snapshots (id, order_id, delivery_id, snapshot_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id("ev"), order_id, delivery_id, snapshot_type, json.dumps(payload, ensure_ascii=False, default=str)),
        )

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


class DisputeService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)

    def open_dispute(
        self,
        *,
        opened_by_telegram_user_id: str | int,
        delivery_id: str,
        reason: str,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            opener = self.accounts.get_or_create_by_telegram(conn, opened_by_telegram_user_id, "advertiser")
            delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            if not delivery:
                raise NotFound(f"delivery not found: {delivery_id}")
            dispute_id = new_id("disp")
            conn.execute(
                """
                INSERT INTO disputes (id, order_id, delivery_id, opened_by_account_id, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (dispute_id, delivery["order_id"], delivery_id, opener["id"], reason),
            )
            conn.execute(
                "UPDATE deliveries SET status = 'disputed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (delivery_id,),
            )
            conn.execute(
                """
                INSERT INTO evidence_snapshots (id, order_id, delivery_id, snapshot_type, payload_json)
                VALUES (?, ?, ?, 'dispute_opened', ?)
                """,
                (
                    new_id("ev"),
                    delivery["order_id"],
                    delivery_id,
                    json.dumps({"reason": reason, "opened_by": opener["id"]}, ensure_ascii=False),
                ),
            )
            return dict(conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone())

    def list_disputes(self, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE d.status = ?"
            params.append(status)
        params.append(limit)
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT d.*, c.title AS channel_title, del.message_id, del.status AS delivery_status
                FROM disputes d
                JOIN deliveries del ON del.id = d.delivery_id
                JOIN channels c ON c.id = del.channel_id
                {where}
                ORDER BY d.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def resolve_dispute(
        self,
        *,
        dispute_id: str,
        resolution: str,
        actor_account_id: str | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            dispute = conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
            if not dispute:
                raise NotFound(f"dispute not found: {dispute_id}")
            if dispute["status"] != "open":
                raise InvalidState(f"争议状态不能裁决: {dispute['status']}")
            conn.execute(
                """
                UPDATE disputes
                SET status = 'resolved', resolution = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (resolution, dispute_id),
            )
            if dispute["delivery_id"]:
                conn.execute(
                    "UPDATE deliveries SET status = 'sent', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'disputed'",
                    (dispute["delivery_id"],),
                )
            conn.execute(
                """
                INSERT INTO audit_logs (id, actor_account_id, action, entity_type, entity_id, payload_json)
                VALUES (?, ?, 'dispute_resolved', 'dispute', ?, ?)
                """,
                (new_id("aud"), actor_account_id, dispute_id, json.dumps({"resolution": resolution}, ensure_ascii=False)),
            )
            return dict(conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone())


class SubscriptionService:
    BASE_SUBSCRIBER_BLOCK = 5_000
    BASE_MONTHLY_PRICE_CENTS = 500
    EXTRA_BLOCK_SIZE = 5_000
    EXTRA_BLOCK_PRICE_CENTS = 100

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.channels = ChannelService(db, settings)
        self.ledger = LedgerService(db, settings)

    @classmethod
    def monthly_price_cents(cls, subscriber_count: int) -> int:
        subscriber_count = max(0, subscriber_count)
        extra_subscribers = max(0, subscriber_count - cls.BASE_SUBSCRIBER_BLOCK)
        extra_blocks = (extra_subscribers + cls.EXTRA_BLOCK_SIZE - 1) // cls.EXTRA_BLOCK_SIZE
        return cls.BASE_MONTHLY_PRICE_CENTS + extra_blocks * cls.EXTRA_BLOCK_PRICE_CENTS

    @staticmethod
    def has_active_subscription(conn: sqlite3.Connection, channel_id: str) -> bool:
        row = conn.execute(
            """
            SELECT id
            FROM channel_subscriptions
            WHERE channel_id = ?
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (channel_id, iso()),
        ).fetchone()
        return row is not None

    def quote(self, subscriber_count: int) -> dict[str, Any]:
        return {
            "subscriber_count": max(0, subscriber_count),
            "monthly_price_cents": self.monthly_price_cents(subscriber_count),
            "formula": "1-5000人 $5/月；超过后每开始新增5000人 +$1/月",
        }

    def activate(
        self,
        *,
        channel_id: str,
        subscriber_count: int,
        months: int = 1,
    ) -> dict[str, Any]:
        months = max(1, months)
        monthly_price = self.monthly_price_cents(subscriber_count)
        starts_at = utcnow()
        expires_at = starts_at + timedelta(days=30 * months)
        with self.db.transaction() as conn:
            self.channels.get_channel(conn, channel_id)
            conn.execute(
                """
                UPDATE channel_subscriptions
                SET status = 'expired'
                WHERE channel_id = ? AND status = 'active'
                """,
                (channel_id,),
            )
            subscription_id = new_id("sub")
            conn.execute(
                """
                INSERT INTO channel_subscriptions (
                    id, channel_id, status, plan, subscriber_count,
                    monthly_price_cents, starts_at, expires_at
                )
                VALUES (?, ?, 'active', 'publisher_premium', ?, ?, ?, ?)
                """,
                (
                    subscription_id,
                    channel_id,
                    max(0, subscriber_count),
                    monthly_price,
                    iso(starts_at),
                    iso(expires_at),
                ),
            )
            return dict(conn.execute("SELECT * FROM channel_subscriptions WHERE id = ?", (subscription_id,)).fetchone())

    def purchase(
        self,
        *,
        channel_id: str,
        subscriber_count: int,
        months: int = 1,
    ) -> dict[str, Any]:
        months = max(1, months)
        monthly_price = self.monthly_price_cents(subscriber_count)
        total_price = monthly_price * months
        starts_at = utcnow()
        expires_at = starts_at + timedelta(days=30 * months)
        with self.db.transaction() as conn:
            channel = self.channels.get_channel(conn, channel_id)
            self.ledger.charge_subscription(
                conn,
                payer_account_id=channel["owner_account_id"],
                channel_id=channel_id,
                amount_cents=total_price,
                months=months,
                subscriber_count=max(0, subscriber_count),
            )
            conn.execute(
                """
                UPDATE channel_subscriptions
                SET status = 'expired'
                WHERE channel_id = ? AND status = 'active'
                """,
                (channel_id,),
            )
            subscription_id = new_id("sub")
            conn.execute(
                """
                INSERT INTO channel_subscriptions (
                    id, channel_id, status, plan, subscriber_count,
                    monthly_price_cents, starts_at, expires_at
                )
                VALUES (?, ?, 'active', 'publisher_premium', ?, ?, ?, ?)
                """,
                (
                    subscription_id,
                    channel_id,
                    max(0, subscriber_count),
                    monthly_price,
                    iso(starts_at),
                    iso(expires_at),
                ),
            )
            subscription = dict(conn.execute("SELECT * FROM channel_subscriptions WHERE id = ?", (subscription_id,)).fetchone())
            subscription["charged_cents"] = total_price
            return subscription

    def get_active(self, channel_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM channel_subscriptions
                WHERE channel_id = ?
                  AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (channel_id, iso()),
            ).fetchone()
            return row_to_dict(row)


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


class AdvertiserSubscriptionService:
    PLANS = {
        "pro": {
            "monthly_price_cents": 1_900,
            "discover_limit": 50,
            "alerts": True,
            "batch_orders": True,
            "full_report": True,
        },
        "enterprise": {
            "monthly_price_cents": 9_900,
            "discover_limit": 500,
            "alerts": True,
            "batch_orders": True,
            "full_report": True,
        },
    }
    FREE_LIMITS = {
        "discover_limit": 5,
        "alerts": False,
        "batch_orders": False,
        "full_report": False,
    }

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.ledger = LedgerService(db, settings)

    def quote(self, plan: str) -> dict[str, Any]:
        if plan not in self.PLANS:
            raise NotFound(f"unknown advertiser plan: {plan}")
        return {"plan": plan, **self.PLANS[plan]}

    def purchase(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        plan: str,
        months: int = 1,
    ) -> dict[str, Any]:
        if plan not in self.PLANS:
            raise NotFound(f"unknown advertiser plan: {plan}")
        months = max(1, months)
        monthly_price = self.PLANS[plan]["monthly_price_cents"]
        total_price = monthly_price * months
        starts_at = utcnow()
        expires_at = starts_at + timedelta(days=30 * months)
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            self.ledger.charge_advertiser_subscription(
                conn,
                advertiser_account_id=advertiser["id"],
                plan=plan,
                amount_cents=total_price,
                months=months,
            )
            conn.execute(
                """
                UPDATE advertiser_subscriptions
                SET status = 'expired'
                WHERE advertiser_account_id = ? AND status = 'active'
                """,
                (advertiser["id"],),
            )
            subscription_id = new_id("adsub")
            conn.execute(
                """
                INSERT INTO advertiser_subscriptions (
                    id, advertiser_account_id, status, plan,
                    monthly_price_cents, starts_at, expires_at
                )
                VALUES (?, ?, 'active', ?, ?, ?, ?)
                """,
                (subscription_id, advertiser["id"], plan, monthly_price, iso(starts_at), iso(expires_at)),
            )
            subscription = dict(conn.execute("SELECT * FROM advertiser_subscriptions WHERE id = ?", (subscription_id,)).fetchone())
            subscription["charged_cents"] = total_price
            return subscription

    def status(self, advertiser_telegram_user_id: str | int) -> dict[str, Any]:
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            active = self.get_active_by_account(conn, advertiser["id"])
            entitlements = self.entitlements_for_account(conn, advertiser["id"])
            return {
                "advertiser_account_id": advertiser["id"],
                "subscription": row_to_dict(active),
                "entitlements": entitlements,
            }

    def get_active_by_telegram(self, telegram_user_id: str | int) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, telegram_user_id, "advertiser")
            row = self.get_active_by_account(conn, advertiser["id"])
            return row_to_dict(row)

    def get_active_by_account(self, conn: sqlite3.Connection, advertiser_account_id: str) -> sqlite3.Row | None:
        self._expire_stale(conn, advertiser_account_id)
        return conn.execute(
            """
            SELECT *
            FROM advertiser_subscriptions
            WHERE advertiser_account_id = ?
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (advertiser_account_id, iso()),
        ).fetchone()

    def entitlements_for_account(self, conn: sqlite3.Connection, advertiser_account_id: str) -> dict[str, Any]:
        active = self.get_active_by_account(conn, advertiser_account_id)
        if not active:
            return {"plan": "free", **self.FREE_LIMITS}
        return {"plan": active["plan"], **self.PLANS[active["plan"]]}

    def require_feature(self, conn: sqlite3.Connection, advertiser_account_id: str, feature: str) -> dict[str, Any]:
        entitlements = self.entitlements_for_account(conn, advertiser_account_id)
        if not entitlements.get(feature):
            raise InvalidState(f"该功能需要广告主高级服务订阅：{feature}")
        return entitlements

    def _expire_stale(self, conn: sqlite3.Connection, advertiser_account_id: str) -> None:
        conn.execute(
            """
            UPDATE advertiser_subscriptions
            SET status = 'expired'
            WHERE advertiser_account_id = ?
              AND status = 'active'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (advertiser_account_id, iso()),
        )


class StarsPaymentService:
    CURRENCY = "XTR"

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings)
        self.ledger = LedgerService(db, settings)

    def create_balance_topup_invoice(
        self,
        *,
        telegram_user_id: str | int,
        stars_amount: int,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        stars_amount = int(stars_amount)
        if stars_amount <= 0:
            raise InvalidState("Stars 充值数量必须大于 0")
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, telegram_user_id, "advertiser", display_name)
            intent = self._create_intent(
                conn,
                buyer_account_id=account["id"],
                telegram_user_id=telegram_user_id,
                kind="balance_topup",
                stars_amount=stars_amount,
                internal_amount_cents=stars_amount * self._star_credit_cents(),
                metadata={"display_name": display_name},
            )
            return self._invoice_response(intent, "插播余额充值", f"充值 {stars_amount} Telegram Stars 到插播余额")

    def create_publisher_subscription_invoice(
        self,
        *,
        channel_id: str,
        subscriber_count: int,
        months: int = 1,
    ) -> dict[str, Any]:
        months = max(1, months)
        subscriber_count = max(0, subscriber_count)
        monthly_price = SubscriptionService.monthly_price_cents(subscriber_count)
        total_cents = monthly_price * months
        with self.db.transaction() as conn:
            channel = self.channels.get_channel(conn, channel_id)
            owner = self.accounts.get(conn, channel["owner_account_id"])
            if not owner.get("telegram_user_id"):
                raise InvalidState("频道主账号缺少 Telegram 用户 ID，无法发送 Stars 发票")
            intent = self._create_intent(
                conn,
                buyer_account_id=owner["id"],
                telegram_user_id=owner["telegram_user_id"],
                kind="publisher_subscription",
                stars_amount=self._stars_for_cents(total_cents),
                internal_amount_cents=total_cents,
                target_channel_id=channel_id,
                plan="publisher_premium",
                months=months,
                subscriber_count=subscriber_count,
                metadata={"channel_title": channel["title"], "monthly_price_cents": monthly_price},
            )
            return self._invoice_response(intent, "插播频道高级订阅", f"{channel['title']} 高级功能 {months} 个月")

    def create_advertiser_subscription_invoice(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        plan: str,
        months: int = 1,
    ) -> dict[str, Any]:
        months = max(1, months)
        if plan not in AdvertiserSubscriptionService.PLANS:
            raise NotFound(f"unknown advertiser plan: {plan}")
        monthly_price = AdvertiserSubscriptionService.PLANS[plan]["monthly_price_cents"]
        total_cents = monthly_price * months
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            intent = self._create_intent(
                conn,
                buyer_account_id=advertiser["id"],
                telegram_user_id=advertiser_telegram_user_id,
                kind="advertiser_subscription",
                stars_amount=self._stars_for_cents(total_cents),
                internal_amount_cents=total_cents,
                plan=plan,
                months=months,
                metadata={"monthly_price_cents": monthly_price},
            )
            return self._invoice_response(intent, "插播广告主高级服务", f"{plan} 套餐 {months} 个月")

    def validate_pre_checkout(self, query: dict[str, Any]) -> dict[str, Any]:
        payload = query.get("invoice_payload", "")
        user = query.get("from") or {}
        telegram_user_id = user.get("id")
        with self.db.transaction() as conn:
            intent = conn.execute("SELECT * FROM stars_payment_intents WHERE payload = ?", (payload,)).fetchone()
            if not intent:
                return {"ok": False, "error_message": "插播支付单不存在或已过期"}
            if intent["status"] != "pending":
                return {"ok": False, "error_message": "插播支付单已处理，请重新发起"}
            if query.get("currency") != self.CURRENCY:
                return {"ok": False, "error_message": "插播仅支持 Telegram Stars 支付"}
            if int(query.get("total_amount", 0)) != intent["stars_amount"]:
                return {"ok": False, "error_message": "插播支付金额不匹配，请重新发起"}
            if telegram_user_id and str(telegram_user_id) != intent["telegram_user_id"]:
                return {"ok": False, "error_message": "该插播支付单不属于当前用户"}
            return {"ok": True, "intent": dict(intent)}

    def fulfill_successful_payment(self, payment: dict[str, Any], *, telegram_user_id: str | int | None = None) -> dict[str, Any]:
        payload = payment.get("invoice_payload", "")
        with self.db.transaction() as conn:
            intent = conn.execute("SELECT * FROM stars_payment_intents WHERE payload = ?", (payload,)).fetchone()
            if not intent:
                raise NotFound("Stars payment intent not found")
            if intent["status"] == "fulfilled":
                return {"handled": True, "type": "stars_payment_already_fulfilled", "intent_id": intent["id"], "kind": intent["kind"]}
            if intent["status"] != "pending":
                raise InvalidState("插播支付单状态不可履约")
            if payment.get("currency") != self.CURRENCY:
                raise InvalidState("插播仅支持 Telegram Stars 支付")
            if int(payment.get("total_amount", 0)) != intent["stars_amount"]:
                raise InvalidState("Stars 支付金额与插播支付单不匹配")
            if telegram_user_id and str(telegram_user_id) != intent["telegram_user_id"]:
                raise InvalidState("Stars 支付用户与插播支付单不匹配")
            charge_id = payment.get("telegram_payment_charge_id")
            if not charge_id:
                raise InvalidState("Stars 支付缺少 Telegram 支付流水")
            duplicate = conn.execute(
                """
                SELECT id
                FROM stars_payment_intents
                WHERE telegram_payment_charge_id = ? AND id != ?
                """,
                (charge_id, intent["id"]),
            ).fetchone()
            if duplicate:
                raise InvalidState("Telegram Stars 支付流水已被其他插播支付单使用")

            conn.execute(
                """
                UPDATE stars_payment_intents
                SET status = 'paid',
                    telegram_payment_charge_id = ?,
                    provider_payment_charge_id = ?,
                    paid_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    charge_id,
                    payment.get("provider_payment_charge_id"),
                    iso(),
                    intent["id"],
                ),
            )
            self.ledger.stars_topup(conn, intent["buyer_account_id"], intent["internal_amount_cents"], charge_id)

            fulfilled: dict[str, Any] = {}
            if intent["kind"] == "publisher_subscription":
                fulfilled = self._fulfill_publisher_subscription(conn, dict(intent), payment)
            elif intent["kind"] == "advertiser_subscription":
                fulfilled = self._fulfill_advertiser_subscription(conn, dict(intent), payment)
            else:
                fulfilled = {"account_id": intent["buyer_account_id"], "amount_cents": intent["internal_amount_cents"]}

            conn.execute(
                """
                UPDATE stars_payment_intents
                SET status = 'fulfilled', fulfilled_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (iso(), intent["id"]),
            )
            return {
                "handled": True,
                "type": f"stars_{intent['kind']}",
                "intent_id": intent["id"],
                "kind": intent["kind"],
                "stars_amount": intent["stars_amount"],
                "internal_amount_cents": intent["internal_amount_cents"],
                **fulfilled,
            }

    def get_intent(self, intent_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM stars_payment_intents WHERE id = ? OR payload = ?",
                (intent_id, intent_id),
            ).fetchone()
            if not row:
                raise NotFound(f"Stars payment intent not found: {intent_id}")
            return dict(row)

    def _fulfill_publisher_subscription(self, conn: sqlite3.Connection, intent: dict[str, Any], payment: dict[str, Any]) -> dict[str, Any]:
        channel_id = intent["target_channel_id"]
        if not channel_id:
            raise InvalidState("频道高级订阅支付单缺少频道")
        subscriber_count = int(intent["subscriber_count"] or 0)
        months = int(intent["months"])
        self.ledger.charge_subscription(
            conn,
            payer_account_id=intent["buyer_account_id"],
            channel_id=channel_id,
            amount_cents=intent["internal_amount_cents"],
            months=months,
            subscriber_count=subscriber_count,
        )
        starts_at = utcnow()
        expires_at = self._subscription_expires_at(payment, months, starts_at)
        conn.execute("UPDATE channel_subscriptions SET status = 'expired' WHERE channel_id = ? AND status = 'active'", (channel_id,))
        subscription_id = new_id("sub")
        monthly_price = intent["internal_amount_cents"] // months
        conn.execute(
            """
            INSERT INTO channel_subscriptions (
                id, channel_id, status, plan, subscriber_count,
                monthly_price_cents, starts_at, expires_at
            )
            VALUES (?, ?, 'active', 'publisher_premium', ?, ?, ?, ?)
            """,
            (subscription_id, channel_id, subscriber_count, monthly_price, iso(starts_at), iso(expires_at)),
        )
        return {"subscription_id": subscription_id, "channel_id": channel_id}

    def _fulfill_advertiser_subscription(self, conn: sqlite3.Connection, intent: dict[str, Any], payment: dict[str, Any]) -> dict[str, Any]:
        plan = intent["plan"]
        if plan not in AdvertiserSubscriptionService.PLANS:
            raise InvalidState("广告主高级服务支付单套餐无效")
        months = int(intent["months"])
        self.ledger.charge_advertiser_subscription(
            conn,
            advertiser_account_id=intent["buyer_account_id"],
            plan=plan,
            amount_cents=intent["internal_amount_cents"],
            months=months,
        )
        starts_at = utcnow()
        expires_at = self._subscription_expires_at(payment, months, starts_at)
        conn.execute(
            "UPDATE advertiser_subscriptions SET status = 'expired' WHERE advertiser_account_id = ? AND status = 'active'",
            (intent["buyer_account_id"],),
        )
        subscription_id = new_id("adsub")
        monthly_price = intent["internal_amount_cents"] // months
        conn.execute(
            """
            INSERT INTO advertiser_subscriptions (
                id, advertiser_account_id, status, plan,
                monthly_price_cents, starts_at, expires_at
            )
            VALUES (?, ?, 'active', ?, ?, ?, ?)
            """,
            (subscription_id, intent["buyer_account_id"], plan, monthly_price, iso(starts_at), iso(expires_at)),
        )
        return {"subscription_id": subscription_id, "plan": plan}

    def _create_intent(
        self,
        conn: sqlite3.Connection,
        *,
        buyer_account_id: str,
        telegram_user_id: str | int,
        kind: str,
        stars_amount: int,
        internal_amount_cents: int,
        target_channel_id: str | None = None,
        plan: str | None = None,
        months: int = 1,
        subscriber_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        intent_id = new_id("spi")
        payload = f"stars:{intent_id}"
        conn.execute(
            """
            INSERT INTO stars_payment_intents (
                id, payload, buyer_account_id, telegram_user_id, kind,
                stars_amount, internal_amount_cents, target_channel_id,
                plan, months, subscriber_count, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                payload,
                buyer_account_id,
                str(telegram_user_id),
                kind,
                stars_amount,
                internal_amount_cents,
                target_channel_id,
                plan,
                months,
                subscriber_count,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        return dict(conn.execute("SELECT * FROM stars_payment_intents WHERE id = ?", (intent_id,)).fetchone())

    def _invoice_response(self, intent: dict[str, Any], title: str, description: str) -> dict[str, Any]:
        invoice = {
            "title": title[:32],
            "description": description[:255],
            "payload": intent["payload"],
            "currency": self.CURRENCY,
            "prices": [{"label": title[:32], "amount": intent["stars_amount"]}],
        }
        return {"intent": intent, "invoice": invoice}

    def _stars_for_cents(self, amount_cents: int) -> int:
        rate = self._star_credit_cents()
        return max(1, (amount_cents + rate - 1) // rate)

    def _star_credit_cents(self) -> int:
        return max(1, int(self.settings.star_credit_cents))

    def _subscription_expires_at(self, payment: dict[str, Any], months: int, starts_at: datetime) -> datetime:
        expiration = payment.get("subscription_expiration_date")
        if expiration:
            return datetime.fromtimestamp(int(expiration), timezone.utc)
        return starts_at + timedelta(days=30 * months)


class AdvertiserService:
    RISK_ORDER = {
        "normal": 0,
        "watch": 1,
        "high": 2,
        "blocked": 3,
    }

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings)
        self.pricing = PricingService(db, settings)
        self.orders = OrderService(db, settings)
        self.subscriptions = AdvertiserSubscriptionService(db, settings)

    def discover_channels(
        self,
        *,
        advertiser_telegram_user_id: str | int | None = None,
        category: str | None = None,
        min_score: int = 0,
        max_risk_level: str = "watch",
        max_price_cents: int | None = None,
        slot_type: str = "standard_card",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        slot_type = self.channels.normalize_slot_type(slot_type)
        requested_limit = limit
        if advertiser_telegram_user_id is not None:
            with self.db.transaction() as conn:
                advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
                entitlements = self.subscriptions.entitlements_for_account(conn, advertiser["id"])
                requested_limit = min(limit, entitlements["discover_limit"])
        else:
            requested_limit = min(limit, AdvertiserSubscriptionService.FREE_LIMITS["discover_limit"])
        rows = self._latest_assessments(category=category, min_score=min_score, max_risk_level=max_risk_level)
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                quote = self.pricing.quote_channel(row["channel_id"], slot_type)
            except ChaboError:
                continue
            if max_price_cents is not None and quote["list_price_cents"] > max_price_cents:
                continue
            results.append(
                {
                    "channel_id": row["channel_id"],
                    "title": row["title"],
                    "username": row["username"],
                    "category": row["category"],
                    "score": row["score"],
                    "risk_level": row["risk_level"],
                    "subscribers": row["subscribers"],
                    "median_24h_views": row["median_24h_views"],
                    "slot_type": slot_type,
                    "list_price_cents": quote["list_price_cents"],
                }
            )
            if len(results) >= requested_limit:
                break
        return results

    def save_channel(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        channel_id: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            self.channels.get_channel(conn, channel_id)
            saved_id = new_id("save")
            conn.execute(
                """
                INSERT INTO advertiser_saved_channels (id, advertiser_account_id, channel_id, note)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(advertiser_account_id, channel_id)
                DO UPDATE SET note = excluded.note
                """,
                (saved_id, advertiser["id"], channel_id, note),
            )
            return dict(
                conn.execute(
                    """
                    SELECT s.*, c.title, c.username
                    FROM advertiser_saved_channels s
                    JOIN channels c ON c.id = s.channel_id
                    WHERE s.advertiser_account_id = ? AND s.channel_id = ?
                    """,
                    (advertiser["id"], channel_id),
                ).fetchone()
            )

    def list_saved_channels(self, advertiser_telegram_user_id: str | int) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            rows = conn.execute(
                """
                SELECT s.*, c.title, c.username
                FROM advertiser_saved_channels s
                JOIN channels c ON c.id = s.channel_id
                WHERE s.advertiser_account_id = ?
                ORDER BY s.created_at DESC
                """,
                (advertiser["id"],),
            ).fetchall()
            return [dict(row) for row in rows]

    def create_alert_rule(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        category: str | None = None,
        min_score: int = 70,
        max_risk_level: str = "normal",
        max_price_cents: int | None = None,
        slot_type: str = "standard_card",
    ) -> dict[str, Any]:
        slot_type = self.channels.normalize_slot_type(slot_type)
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            self.subscriptions.require_feature(conn, advertiser["id"], "alerts")
            rule_id = new_id("alert")
            conn.execute(
                """
                INSERT INTO advertiser_alert_rules (
                    id, advertiser_account_id, category, min_score,
                    max_risk_level, max_price_cents, slot_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (rule_id, advertiser["id"], category, min_score, max_risk_level, max_price_cents, slot_type),
            )
            return dict(conn.execute("SELECT * FROM advertiser_alert_rules WHERE id = ?", (rule_id,)).fetchone())

    def scan_alerts(self, advertiser_telegram_user_id: str | int | None = None) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        with self.db.transaction() as conn:
            params: list[Any] = []
            where = "r.status = 'active'"
            if advertiser_telegram_user_id is not None:
                advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
                self.subscriptions.require_feature(conn, advertiser["id"], "alerts")
                where += " AND r.advertiser_account_id = ?"
                params.append(advertiser["id"])
            rules = conn.execute(
                f"SELECT r.* FROM advertiser_alert_rules r WHERE {where} ORDER BY r.created_at",
                params,
            ).fetchall()
            for rule in rules:
                if not self.subscriptions.entitlements_for_account(conn, rule["advertiser_account_id"]).get("alerts"):
                    continue
                candidates = self._latest_assessments_with_conn(
                    conn,
                    category=rule["category"],
                    min_score=rule["min_score"],
                    max_risk_level=rule["max_risk_level"],
                )
                for channel in candidates:
                    try:
                        quote = self._quote_from_assessment_conn(conn, channel, rule["slot_type"])
                    except ChaboError:
                        continue
                    if rule["max_price_cents"] is not None and quote["list_price_cents"] > rule["max_price_cents"]:
                        continue
                    event_id = new_id("evt")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO advertiser_alert_events (
                            id, rule_id, advertiser_account_id, channel_id, assessment_id
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (event_id, rule["id"], rule["advertiser_account_id"], channel["channel_id"], channel["assessment_id"]),
                    )
                    event = conn.execute(
                        """
                        SELECT e.*, c.title, c.username
                        FROM advertiser_alert_events e
                        JOIN channels c ON c.id = e.channel_id
                        WHERE e.rule_id = ? AND e.channel_id = ? AND e.assessment_id = ?
                        """,
                        (rule["id"], channel["channel_id"], channel["assessment_id"]),
                    ).fetchone()
                    if event:
                        created.append(dict(event))
        return created

    def list_alert_events(self, advertiser_telegram_user_id: str | int, status: str | None = "new") -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            self.subscriptions.require_feature(conn, advertiser["id"], "alerts")
            params: list[Any] = [advertiser["id"]]
            where = "e.advertiser_account_id = ?"
            if status:
                where += " AND e.status = ?"
                params.append(status)
            rows = conn.execute(
                f"""
                SELECT e.*, c.title, c.username, a.score, a.category, a.risk_level
                FROM advertiser_alert_events e
                JOIN channels c ON c.id = e.channel_id
                JOIN channel_pricing_assessments a ON a.id = e.assessment_id
                WHERE {where}
                ORDER BY e.created_at DESC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def report(self, advertiser_telegram_user_id: str | int) -> dict[str, Any]:
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            entitlements = self.subscriptions.entitlements_for_account(conn, advertiser["id"])
            summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS orders_count,
                    COALESCE(SUM(budget_cents), 0) AS total_budget_cents,
                    COALESCE(SUM(reserved_cents), 0) AS reserved_cents,
                    COALESCE(SUM(spent_cents), 0) AS spent_cents
                FROM ad_orders
                WHERE advertiser_account_id = ?
                """,
                (advertiser["id"],),
            ).fetchone()
            deliveries = conn.execute(
                """
                SELECT
                    COUNT(d.id) AS deliveries_count,
                    COALESCE(SUM(CASE WHEN d.status IN ('sent', 'confirmed', 'disputed') THEN 1 ELSE 0 END), 0) AS sent_count,
                    COALESCE(SUM(d.charge_cents), 0) AS charged_cents
                FROM deliveries d
                JOIN ad_orders o ON o.id = d.order_id
                WHERE o.advertiser_account_id = ?
                """,
                (advertiser["id"],),
            ).fetchone()
            starts = conn.execute(
                """
                SELECT COUNT(*) AS bot_starts
                FROM metric_snapshots m
                JOIN deliveries d ON d.id = m.delivery_id
                JOIN ad_orders o ON o.id = d.order_id
                WHERE o.advertiser_account_id = ?
                  AND m.metric_type = 'bot_start'
                """,
                (advertiser["id"],),
            ).fetchone()
            by_channel = conn.execute(
                """
                SELECT
                    c.id AS channel_id,
                    c.title,
                    COUNT(o.id) AS orders_count,
                    COALESCE(SUM(o.spent_cents), 0) AS spent_cents,
                    COALESCE(SUM(d.charge_cents), 0) AS charged_cents
                FROM ad_orders o
                JOIN channels c ON c.id = o.channel_id
                LEFT JOIN deliveries d ON d.order_id = o.id
                WHERE o.advertiser_account_id = ?
                GROUP BY c.id, c.title
                ORDER BY spent_cents DESC
                """,
                (advertiser["id"],),
            ).fetchall()
            return {
                "advertiser_account_id": advertiser["id"],
                "orders_count": summary["orders_count"],
                "total_budget_cents": summary["total_budget_cents"],
                "reserved_cents": summary["reserved_cents"],
                "spent_cents": summary["spent_cents"],
                "deliveries_count": deliveries["deliveries_count"],
                "sent_count": deliveries["sent_count"],
                "charged_cents": deliveries["charged_cents"],
                "bot_starts": starts["bot_starts"],
                "by_channel": [dict(row) for row in by_channel] if entitlements["full_report"] else [],
                "plan": entitlements["plan"],
                "limited": not entitlements["full_report"],
            }

    def create_batch_orders(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        channel_tokens: list[str],
        slot_type: str,
        text: str,
        target_url: str,
        budget_cents: int,
        button_text: str = "查看详情",
        category: str = "general",
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            self.subscriptions.require_feature(conn, advertiser["id"], "batch_orders")
        results: list[dict[str, Any]] = []
        for token in channel_tokens:
            try:
                order = self.orders.create_order(
                    advertiser_telegram_user_id=advertiser_telegram_user_id,
                    channel_token=token,
                    slot_type=slot_type,
                    text=text,
                    target_url=target_url,
                    budget_cents=budget_cents,
                    button_text=button_text,
                    category=category,
                    campaign_name="批量插播广告",
                )
                results.append({"channel_token": token, "ok": True, "order_id": order["id"]})
            except ChaboError as exc:
                results.append({"channel_token": token, "ok": False, "error": str(exc)})
        return {
            "created_count": sum(1 for item in results if item["ok"]),
            "failed_count": sum(1 for item in results if not item["ok"]),
            "results": results,
        }

    def _latest_assessments(
        self,
        *,
        category: str | None,
        min_score: int,
        max_risk_level: str,
    ) -> list[sqlite3.Row]:
        with self.db.transaction() as conn:
            return self._latest_assessments_with_conn(
                conn,
                category=category,
                min_score=min_score,
                max_risk_level=max_risk_level,
            )

    def _latest_assessments_with_conn(
        self,
        conn: sqlite3.Connection,
        *,
        category: str | None,
        min_score: int,
        max_risk_level: str,
    ) -> list[sqlite3.Row]:
        max_risk_rank = self.RISK_ORDER.get(max_risk_level, self.RISK_ORDER["watch"])
        params: list[Any] = [min_score]
        category_filter = ""
        if category:
            category_filter = "AND a.category = ?"
            params.append(category)
        rows = conn.execute(
            f"""
            SELECT
                a.id AS assessment_id,
                a.channel_id,
                a.category,
                a.score,
                a.risk_level,
                a.base_standard_price_cents,
                a.subscribers,
                a.median_24h_views,
                c.title,
                c.username
            FROM channel_pricing_assessments a
            JOIN (
                SELECT channel_id, MAX(created_at) AS created_at
                FROM channel_pricing_assessments
                GROUP BY channel_id
            ) latest ON latest.channel_id = a.channel_id AND latest.created_at = a.created_at
            JOIN channels c ON c.id = a.channel_id
            WHERE a.score >= ?
              {category_filter}
            ORDER BY a.score DESC, a.created_at DESC
            """,
            params,
        ).fetchall()
        return [row for row in rows if self.RISK_ORDER.get(row["risk_level"], 3) <= max_risk_rank]

    def _quote_from_assessment_conn(
        self,
        conn: sqlite3.Connection,
        assessment: sqlite3.Row,
        slot_type: str,
    ) -> dict[str, Any]:
        slot_type = self.channels.normalize_slot_type(slot_type)
        self.channels._ensure_default_format_policies(conn, assessment["channel_id"])
        policy = conn.execute(
            "SELECT * FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = ?",
            (assessment["channel_id"], slot_type),
        ).fetchone()
        if not policy:
            raise NotFound(f"unknown ad format: {slot_type}")
        if not policy["enabled"]:
            raise InvalidState("该频道主当前未开启这种插播广告形态")
        band = policy["owner_price_band"]
        band_bps = policy["custom_multiplier_bps"] if band == "custom" else PricingService.BAND_FACTORS_BPS.get(band, 10000)
        format_bps = PricingService.FORMAT_FACTORS_BPS[slot_type]
        list_price = assessment["base_standard_price_cents"] * format_bps // 10_000
        list_price = list_price * band_bps // 10_000
        return {
            "channel_id": assessment["channel_id"],
            "slot_type": slot_type,
            "list_price_cents": list_price,
        }


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


class PriceOfferService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings)
        self.pricing = PricingService(db, settings)
        self.ledger = LedgerService(db, settings)

    def create_offer(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        channel_id: str,
        slot_type: str,
        offered_price_cents: int,
        creative_text: str,
        target_url: str,
        budget_cents: int | None = None,
        button_text: str = "查看详情",
        category: str = "general",
        scheduled_at: datetime | None = None,
        end_at: datetime | None = None,
        frequency_per_day: int = 1,
        message: str | None = None,
    ) -> dict[str, Any]:
        slot_type = self.channels.normalize_slot_type(slot_type)
        scheduled_at = scheduled_at or utcnow()
        budget_cents = budget_cents or offered_price_cents
        if budget_cents < offered_price_cents:
            raise InvalidState("砍价预算不能低于报价单次价格")
        if not creative_text.strip():
            raise InvalidState("砍价报价必须包含插播广告文案")
        if not target_url.strip():
            raise InvalidState("砍价报价必须包含广告目标链接")
        quote = self.pricing.quote_channel(channel_id, slot_type)
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            offer_id = new_id("offer")
            conn.execute(
                """
                INSERT INTO price_offers (
                    id, advertiser_account_id, channel_id, slot_type,
                    offered_price_cents, list_price_cents, budget_cents,
                    creative_text, target_url, button_text, category,
                    scheduled_at, end_at, frequency_per_day, message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    offer_id,
                    advertiser["id"],
                    channel_id,
                    slot_type,
                    offered_price_cents,
                    quote["list_price_cents"],
                    budget_cents,
                    creative_text,
                    target_url,
                    button_text,
                    category,
                    iso(scheduled_at),
                    iso(end_at) if end_at else None,
                    frequency_per_day,
                    message,
                ),
            )
            return dict(conn.execute("SELECT * FROM price_offers WHERE id = ?", (offer_id,)).fetchone())

    def respond_offer(self, offer_id: str, *, accepted: bool) -> dict[str, Any]:
        with self.db.transaction() as conn:
            offer = conn.execute("SELECT * FROM price_offers WHERE id = ?", (offer_id,)).fetchone()
            if not offer:
                raise NotFound(f"price offer not found: {offer_id}")
            if offer["status"] != "pending":
                raise InvalidState("该砍价报价已经处理过")
            order_id = None
            if accepted:
                order_id = self._create_order_from_offer(conn, offer)
            conn.execute(
                """
                UPDATE price_offers
                SET status = ?, accepted_order_id = ?, responded_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                ("accepted" if accepted else "rejected", order_id, offer_id),
            )
            return dict(conn.execute("SELECT * FROM price_offers WHERE id = ?", (offer_id,)).fetchone())

    def _create_order_from_offer(self, conn: sqlite3.Connection, offer: sqlite3.Row) -> str:
        channel = self.channels.get_channel(conn, offer["channel_id"])
        rate = self.channels.get_rate(conn, offer["channel_id"], offer["slot_type"])
        policy = conn.execute(
            """
            SELECT * FROM channel_ad_format_policies
            WHERE channel_id = ? AND format_type = ?
            """,
            (offer["channel_id"], offer["slot_type"]),
        ).fetchone()
        if policy and not policy["enabled"]:
            raise InvalidState("该频道主当前未开启这种插播广告形态")
        effective_service_fee_bps = 0 if SubscriptionService.has_active_subscription(conn, channel["id"]) else self.settings.default_service_fee_bps
        if policy and policy["platform_promo_enabled"]:
            effective_service_fee_bps = 0
        conn.execute(
            """
            UPDATE channel_configs
            SET service_fee_bps = ?, updated_at = CURRENT_TIMESTAMP
            WHERE channel_id = ?
            """,
            (effective_service_fee_bps, channel["id"]),
        )
        budget_cents = offer["budget_cents"] or offer["offered_price_cents"]
        if budget_cents < offer["offered_price_cents"]:
            raise InvalidState("砍价预算不能低于报价单次价格")

        advertiser = self.accounts.get(conn, offer["advertiser_account_id"])
        if advertiser["available_balance_cents"] < budget_cents:
            raise InsufficientBalance("广告主插播余额不足，接受砍价后无法冻结预算")

        campaign_id = new_id("camp")
        creative_id = new_id("cre")
        order_id = new_id("ord")
        content_hash = hashlib.sha256(
            f"{offer['creative_text']}|{offer['target_url']}|{offer['button_text']}".encode("utf-8")
        ).hexdigest()
        scheduled_at = offer["scheduled_at"] or iso()
        conn.execute(
            """
            INSERT INTO campaigns (id, advertiser_account_id, name)
            VALUES (?, ?, ?)
            """,
            (campaign_id, offer["advertiser_account_id"], "砍价成交插播广告"),
        )
        conn.execute(
            """
            INSERT INTO creatives (
                id, campaign_id, text, target_url, button_text, category, content_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                creative_id,
                campaign_id,
                offer["creative_text"],
                offer["target_url"],
                offer["button_text"],
                offer["category"],
                content_hash,
            ),
        )
        conn.execute(
            """
            INSERT INTO ad_orders (
                id, campaign_id, creative_id, advertiser_account_id, channel_id, slot_id,
                status, budget_cents, reserved_cents, unit_price_cents, start_at, end_at,
                scheduled_at, frequency_per_day, price_offer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                campaign_id,
                creative_id,
                offer["advertiser_account_id"],
                offer["channel_id"],
                rate["slot_id"],
                budget_cents,
                budget_cents,
                offer["offered_price_cents"],
                scheduled_at,
                offer["end_at"],
                scheduled_at,
                offer["frequency_per_day"] or 1,
                offer["id"],
            ),
        )
        self.ledger.reserve_budget(conn, offer["advertiser_account_id"], order_id, budget_cents, rate["currency"])
        self._snapshot(conn, order_id, None, "creative", {"text": offer["creative_text"], "target_url": offer["target_url"], "button_text": offer["button_text"]})
        self._snapshot(conn, order_id, None, "accepted_price_offer", dict(offer))
        self._snapshot(conn, order_id, None, "rate_card", {**rate, "accepted_unit_price_cents": offer["offered_price_cents"], "price_offer_id": offer["id"]})
        self._snapshot(conn, order_id, None, "channel", channel)
        return order_id

    def _snapshot(
        self,
        conn: sqlite3.Connection,
        order_id: str | None,
        delivery_id: str | None,
        snapshot_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO evidence_snapshots (id, order_id, delivery_id, snapshot_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id("ev"), order_id, delivery_id, snapshot_type, json.dumps(payload, ensure_ascii=False, default=str)),
        )
