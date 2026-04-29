from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..db import Database
from ..ids import new_id, new_ref_token
from ..money import bps_amount

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


class ToolCallLogService:
    """Audit log for AI tool calls and operator actions.

    Designed as a single sink that any AI-callable service method or
    Bot/Admin action can write to. Records who initiated the call (by
    telegram_user_id when available), what tool was invoked, the
    arguments summary, and the result status / error type. The log is
    append-only; callers are expected to redact sensitive fields from
    `arguments` before passing them in.
    """

    ACTOR_KINDS = ("human", "ai", "admin", "system")
    RESULT_STATUSES = ("success", "error")

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def log_call(
        self,
        *,
        tool_name: str,
        result_status: str = "success",
        actor_telegram_user_id: str | int | None = None,
        actor_kind: str = "human",
        session_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        result_summary: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        if actor_kind not in self.ACTOR_KINDS:
            raise InvalidState(f"非法 actor_kind：{actor_kind}")
        if result_status not in self.RESULT_STATUSES:
            raise InvalidState(f"非法 result_status：{result_status}")
        log_id = new_id("tool")
        with self.db.transaction() as conn:
            actor_account_id: str | None = None
            if actor_telegram_user_id is not None:
                row = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(actor_telegram_user_id),),
                ).fetchone()
                if row:
                    actor_account_id = row["id"]
            conn.execute(
                """
                INSERT INTO tool_call_logs (
                    id, actor_account_id, actor_telegram_user_id, actor_kind,
                    session_id, tool_name, arguments_json,
                    result_status, result_summary, error_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    actor_account_id,
                    str(actor_telegram_user_id) if actor_telegram_user_id is not None else None,
                    actor_kind,
                    session_id,
                    tool_name,
                    json.dumps(arguments or {}, ensure_ascii=False),
                    result_status,
                    result_summary,
                    error_type,
                ),
            )
            row = conn.execute(
                "SELECT * FROM tool_call_logs WHERE id = ?", (log_id,)
            ).fetchone()
            return dict(row)

    def list_calls(
        self,
        *,
        actor_telegram_user_id: str | int | None = None,
        tool_name: str | None = None,
        result_status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tool_call_logs WHERE 1 = 1"
        params: list[Any] = []
        if actor_telegram_user_id is not None:
            sql += " AND actor_telegram_user_id = ?"
            params.append(str(actor_telegram_user_id))
        if tool_name is not None:
            sql += " AND tool_name = ?"
            params.append(tool_name)
        if result_status is not None:
            if result_status not in self.RESULT_STATUSES:
                raise InvalidState(f"非法 result_status：{result_status}")
            sql += " AND result_status = ?"
            params.append(result_status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def get_call(self, log_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tool_call_logs WHERE id = ?", (log_id,)
            ).fetchone()
            if not row:
                raise NotFound(f"工具调用日志不存在：{log_id}")
            return dict(row)

    def log_success(
        self,
        *,
        tool_name: str,
        actor_telegram_user_id: str | int | None = None,
        actor_kind: str = "human",
        session_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        return self.log_call(
            tool_name=tool_name,
            actor_telegram_user_id=actor_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=arguments,
            result_status="success",
            result_summary=result_summary,
        )

    def log_failure(
        self,
        *,
        tool_name: str,
        actor_telegram_user_id: str | int | None = None,
        actor_kind: str = "human",
        session_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        error: BaseException,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        return self.log_call(
            tool_name=tool_name,
            actor_telegram_user_id=actor_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=arguments,
            result_status="error",
            error_type=type(error).__name__,
            result_summary=result_summary or str(error)[:200],
        )


class LedgerService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.tool_calls = ToolCallLogService(db, settings)

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

    def get_wallet_summary(
        self,
        *,
        telegram_user_id: str | int,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            account = conn.execute(
                "SELECT id, available_balance_cents, reserved_balance_cents, "
                "       spent_balance_cents "
                "FROM accounts WHERE telegram_user_id = ?",
                (str(telegram_user_id),),
            ).fetchone()
            if not account:
                return {
                    "telegram_user_id": str(telegram_user_id),
                    "available_balance_cents": 0,
                    "reserved_balance_cents": 0,
                    "spent_balance_cents": 0,
                }
            return {
                "telegram_user_id": str(telegram_user_id),
                "account_id": account["id"],
                "available_balance_cents": account["available_balance_cents"],
                "reserved_balance_cents": account["reserved_balance_cents"],
                "spent_balance_cents": account["spent_balance_cents"],
            }

    def get_earnings_summary(
        self,
        *,
        telegram_user_id: str | int,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            account = conn.execute(
                "SELECT id, pending_earnings_cents, confirmed_earnings_cents, "
                "       releasable_earnings_cents "
                "FROM accounts WHERE telegram_user_id = ?",
                (str(telegram_user_id),),
            ).fetchone()
            if not account:
                return {
                    "telegram_user_id": str(telegram_user_id),
                    "pending_earnings_cents": 0,
                    "confirmed_earnings_cents": 0,
                    "releasable_earnings_cents": 0,
                }
            return {
                "telegram_user_id": str(telegram_user_id),
                "account_id": account["id"],
                "pending_earnings_cents": account["pending_earnings_cents"],
                "confirmed_earnings_cents": account["confirmed_earnings_cents"],
                "releasable_earnings_cents": account["releasable_earnings_cents"],
            }

    def list_channel_earnings(
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
                SELECT
                    c.id AS channel_id,
                    c.title AS title,
                    c.ref_token AS ref_token,
                    COALESCE(SUM(CASE WHEN d.status = 'sent' THEN d.publisher_net_cents - d.publisher_reversed_cents ELSE 0 END), 0) AS pending_cents,
                    COALESCE(SUM(CASE WHEN d.status = 'confirmed' THEN d.publisher_net_cents - d.publisher_reversed_cents ELSE 0 END), 0) AS confirmed_cents,
                    COALESCE(SUM(d.platform_fee_cents - d.platform_fee_reversed_cents), 0) AS platform_fee_cents,
                    COUNT(d.id) AS delivery_count
                FROM channels c
                LEFT JOIN deliveries d ON d.channel_id = c.id AND d.status IN ('sent', 'confirmed')
                WHERE c.owner_account_id = ?
                GROUP BY c.id, c.title, c.ref_token
                ORDER BY c.created_at DESC
                """,
                (account["id"],),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_transactions(
        self,
        *,
        telegram_user_id: str | int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            account = conn.execute(
                "SELECT id FROM accounts WHERE telegram_user_id = ?",
                (str(telegram_user_id),),
            ).fetchone()
            if not account:
                return []
            rows = conn.execute(
                """
                SELECT id, type, amount_cents, currency, memo, created_at, order_id, delivery_id
                FROM ledger_transactions
                WHERE account_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (account["id"], int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_reserved_orders(
        self,
        *,
        telegram_user_id: str | int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            account = conn.execute(
                "SELECT id FROM accounts WHERE telegram_user_id = ?",
                (str(telegram_user_id),),
            ).fetchone()
            if not account:
                return []
            rows = conn.execute(
                """
                SELECT o.id AS order_id, o.status, o.reserved_cents, o.budget_cents, o.spent_cents,
                       o.currency, c.title AS channel_title
                FROM ad_orders o
                LEFT JOIN channels c ON c.id = o.channel_id
                WHERE o.advertiser_account_id = ? AND o.reserved_cents > 0
                ORDER BY o.created_at DESC
                LIMIT ?
                """,
                (account["id"], int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def manual_topup(
        self,
        telegram_user_id: str | int,
        amount_cents: int,
        *,
        display_name: str | None = None,
        memo: str = "人工入账",
        actor_telegram_user_id: str | int | None = None,
        actor_kind: str = "admin",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {
            "telegram_user_id": str(telegram_user_id),
            "amount_cents": amount_cents,
            "memo_preview": (memo or "")[:80],
        }
        try:
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
                result = self.accounts.get(conn, account["id"])
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="manual_topup",
                actor_telegram_user_id=actor_telegram_user_id if actor_telegram_user_id is not None else telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="manual_topup",
            actor_telegram_user_id=actor_telegram_user_id if actor_telegram_user_id is not None else telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"+{amount_cents} cents to {telegram_user_id}",
        )
        return result

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


class TopupApprovalService:
    """Two-person approval workflow for manual top-ups.

    The requester and the approver must be different accounts. Approval
    triggers `LedgerService.manual_topup` with the approver recorded as
    the actor; rejection leaves balances untouched. The status enum is
    deliberately small (`pending / approved / rejected`) — `approved` is
    only set after the ledger write succeeds, so anything in `pending`
    is auditable but not yet impactful.
    """

    STATUSES = ("pending", "approved", "rejected")
    MAX_REASON_LEN = 500
    MAX_NOTE_LEN = 500
    MAX_URL_LEN = 1000

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.ledger = LedgerService(db, settings)
        self.tool_calls = ToolCallLogService(db, settings)

    def request_topup(
        self,
        *,
        recipient_telegram_user_id: str | int,
        amount_cents: int,
        reason: str,
        requester_telegram_user_id: str | int,
        evidence_url: str | None = None,
        currency: str = "USD",
        request_note: str | None = None,
        actor_kind: str = "admin",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if amount_cents <= 0:
            raise InvalidState("入账金额必须大于 0")
        cleaned_reason = (reason or "").strip()
        if not cleaned_reason:
            raise InvalidState("入账请求必须填写原因 / 凭证摘要")
        if len(cleaned_reason) > self.MAX_REASON_LEN:
            raise InvalidState(f"原因最长 {self.MAX_REASON_LEN} 字")
        cleaned_evidence = (evidence_url or "").strip() or None
        if cleaned_evidence and len(cleaned_evidence) > self.MAX_URL_LEN:
            raise InvalidState(f"凭证链接最长 {self.MAX_URL_LEN} 字")
        cleaned_request_note = (request_note or "").strip() or None
        if cleaned_request_note and len(cleaned_request_note) > self.MAX_NOTE_LEN:
            raise InvalidState(f"申请备注最长 {self.MAX_NOTE_LEN} 字")
        request_id = new_id("treq")
        try:
            with self.db.transaction() as conn:
                requester = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(requester_telegram_user_id),),
                ).fetchone()
                if not requester:
                    raise NotFound(f"申请人账号不存在：{requester_telegram_user_id}")
                recipient_row = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(recipient_telegram_user_id),),
                ).fetchone()
                recipient_account_id = recipient_row["id"] if recipient_row else None
                conn.execute(
                    """
                    INSERT INTO topup_requests (
                        id, recipient_telegram_user_id, recipient_account_id,
                        amount_cents, currency, reason, evidence_url, status,
                        requester_account_id, request_note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        request_id,
                        str(recipient_telegram_user_id),
                        recipient_account_id,
                        amount_cents,
                        currency,
                        cleaned_reason,
                        cleaned_evidence,
                        requester["id"],
                        cleaned_request_note,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM topup_requests WHERE id = ?", (request_id,)
                ).fetchone()
                request = dict(row)
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="topup_request",
                actor_telegram_user_id=requester_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments={
                    "recipient": str(recipient_telegram_user_id),
                    "amount_cents": amount_cents,
                },
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="topup_request",
            actor_telegram_user_id=requester_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments={
                "recipient": str(recipient_telegram_user_id),
                "amount_cents": amount_cents,
                "has_evidence": bool(cleaned_evidence),
            },
            result_summary=f"requested {request_id}",
        )
        return request

    def approve_topup(
        self,
        *,
        request_id: str,
        approver_telegram_user_id: str | int,
        approval_note: str | None = None,
        actor_kind: str = "admin",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        cleaned_note = (approval_note or "").strip() or None
        if cleaned_note and len(cleaned_note) > self.MAX_NOTE_LEN:
            raise InvalidState(f"审批备注最长 {self.MAX_NOTE_LEN} 字")
        request_snapshot: dict[str, Any] | None = None
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM topup_requests WHERE id = ?", (request_id,)
                ).fetchone()
                if not row:
                    raise NotFound(f"入账请求不存在：{request_id}")
                if row["status"] != "pending":
                    raise InvalidState(f"入账请求当前状态不能审批：{row['status']}")
                approver = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(approver_telegram_user_id),),
                ).fetchone()
                if not approver:
                    raise NotFound(f"审批人账号不存在：{approver_telegram_user_id}")
                if approver["id"] == row["requester_account_id"]:
                    raise InvalidState("审批人必须与申请人是不同账号（双人复核）")
                request_snapshot = dict(row)
                approver_id = approver["id"]
            self.ledger.manual_topup(
                request_snapshot["recipient_telegram_user_id"],
                request_snapshot["amount_cents"],
                memo=f"人工入账：{request_snapshot['reason']}",
                actor_telegram_user_id=approver_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
            )
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE topup_requests
                    SET status = 'approved',
                        approver_account_id = ?,
                        approval_note = ?,
                        settled_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (approver_id, cleaned_note, request_id),
                )
                row = conn.execute(
                    "SELECT * FROM topup_requests WHERE id = ?", (request_id,)
                ).fetchone()
                final = dict(row)
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="topup_approve",
                actor_telegram_user_id=approver_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments={"request_id": request_id, "has_note": bool(cleaned_note)},
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="topup_approve",
            actor_telegram_user_id=approver_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments={"request_id": request_id, "has_note": bool(cleaned_note)},
            result_summary=f"approved {request_id}",
        )
        return final

    def reject_topup(
        self,
        *,
        request_id: str,
        approver_telegram_user_id: str | int,
        approval_note: str | None = None,
        actor_kind: str = "admin",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        cleaned_note = (approval_note or "").strip() or None
        if cleaned_note and len(cleaned_note) > self.MAX_NOTE_LEN:
            raise InvalidState(f"审批备注最长 {self.MAX_NOTE_LEN} 字")
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM topup_requests WHERE id = ?", (request_id,)
                ).fetchone()
                if not row:
                    raise NotFound(f"入账请求不存在：{request_id}")
                if row["status"] != "pending":
                    raise InvalidState(f"入账请求当前状态不能拒绝：{row['status']}")
                approver = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(approver_telegram_user_id),),
                ).fetchone()
                if not approver:
                    raise NotFound(f"审批人账号不存在：{approver_telegram_user_id}")
                if approver["id"] == row["requester_account_id"]:
                    raise InvalidState("审批人必须与申请人是不同账号（双人复核）")
                conn.execute(
                    """
                    UPDATE topup_requests
                    SET status = 'rejected',
                        approver_account_id = ?,
                        approval_note = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (approver["id"], cleaned_note, request_id),
                )
                final = dict(conn.execute(
                    "SELECT * FROM topup_requests WHERE id = ?", (request_id,)
                ).fetchone())
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="topup_reject",
                actor_telegram_user_id=approver_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments={"request_id": request_id, "has_note": bool(cleaned_note)},
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="topup_reject",
            actor_telegram_user_id=approver_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments={"request_id": request_id, "has_note": bool(cleaned_note)},
            result_summary=f"rejected {request_id}",
        )
        return final

    def list_requests(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in self.STATUSES:
            raise InvalidState(f"非法 status：{status}")
        sql = "SELECT * FROM topup_requests"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def get_request(self, request_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM topup_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if not row:
                raise NotFound(f"入账请求不存在：{request_id}")
            return dict(row)


