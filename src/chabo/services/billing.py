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
from .wallet import (
    AccountService,
    LedgerService,
    ToolCallLogService,
)
from .channel import (
    ChannelService,
    PricingService,
)
from .order import (
    MaterialService,
    OrderService,
)


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


