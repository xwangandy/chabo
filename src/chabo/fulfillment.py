from __future__ import annotations

import json
import logging
import sqlite3
import html
from typing import Any

from .config import Settings
from .db import Database
from .ids import new_id
from .money import cents_to_money
from .services import ChannelService, InvalidState, LedgerService, NotFound, OrderService, iso, utcnow
from .telegram import MessageGateway, TelegramError
from .telegram_abc_client import TelegramABCClient


logger = logging.getLogger(__name__)


class FulfillmentService:
    def __init__(self, db: Database, settings: Settings, gateway: MessageGateway):
        self.db = db
        self.settings = settings
        self.gateway = gateway
        self.channels = ChannelService(db, settings, gateway=gateway)
        self.ledger = LedgerService(db, settings)
        self.orders = OrderService(db, settings, gateway=gateway)

    def dispatch_due(self, limit: int = 20) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        now = iso()
        with self.db.transaction() as conn:
            due_rows = conn.execute(
                """
                SELECT d.id
                FROM deliveries d
                JOIN ad_orders o ON o.id = d.order_id
                WHERE d.status = 'scheduled'
                  AND d.scheduled_at <= ?
                  AND o.status IN ('approved', 'running')
                ORDER BY d.scheduled_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            due_ids = [row["id"] for row in due_rows]
        for delivery_id in due_ids:
            outcome = self._dispatch_one_isolated(delivery_id)
            if outcome is not None:
                sent.append(outcome)
        return sent

    def confirm_due_earnings(self, observation_hours: int = 24) -> int:
        count = 0
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM deliveries
                WHERE status = 'sent'
                  AND sent_at <= datetime('now', '-' || ? || ' hours')
                  AND publisher_net_cents > 0
                """,
                (observation_hours,),
            ).fetchall()
            for row in rows:
                self.ledger.confirm_publisher_earning(conn, row["id"])
                conn.execute(
                    "UPDATE deliveries SET status = 'confirmed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
                count += 1
        return count

    def release_due_holdbacks(self, holdback_days: int | None = None) -> int:
        count = 0
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT d.id
                FROM deliveries d
                LEFT JOIN channel_configs cc ON cc.channel_id = d.channel_id
                WHERE d.status = 'confirmed'
                  AND d.holdback_released_at IS NULL
                  AND d.sent_at <= datetime('now', '-' || COALESCE(?, cc.holdback_days, 7) || ' days')
                """,
                (holdback_days,),
            ).fetchall()
            for row in rows:
                released = self.ledger.release_publisher_holdback(conn, row["id"])
                if released > 0 or holdback_days == 0:
                    count += 1
        return count

    def unpublish_due(self, pin_hours: int = 24, now_iso: str | None = None) -> int:
        """Unpin expired pin24h deliveries and delete expired delete_at deliveries."""
        count = 0
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.order_id, d.message_id, c.telegram_chat_id
                FROM deliveries d
                JOIN channels c ON c.id = d.channel_id
                WHERE d.pinned = 1
                  AND d.status IN ('sent', 'confirmed')
                  AND d.sent_at IS NOT NULL
                  AND (
                      (d.unpin_at IS NOT NULL AND d.unpin_at <= COALESCE(?, datetime('now')))
                      OR d.sent_at <= datetime('now', '-' || ? || ' hours')
                  )
                """,
                (now_iso, pin_hours),
            ).fetchall()
            delete_rows = conn.execute(
                """
                SELECT d.id, d.order_id, d.message_id, c.telegram_chat_id
                FROM deliveries d
                JOIN channels c ON c.id = d.channel_id
                WHERE d.delete_at IS NOT NULL
                  AND d.delete_at <= COALESCE(?, datetime('now'))
                  AND d.status IN ('sent', 'confirmed')
                """,
                (now_iso,),
            ).fetchall()
        for row in rows:
            try:
                if row["message_id"] and hasattr(self.gateway, "unpin_message"):
                    self.gateway.unpin_message(
                        chat_id=row["telegram_chat_id"],
                        message_id=row["message_id"],
                    )
            except Exception as exc:
                logger.warning("unpin_message_failed delivery=%s error=%s", row["id"], exc)
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE deliveries SET pinned = 0, unpin_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
                self._snapshot(
                    conn,
                    row["order_id"],
                    row["id"],
                    "pin_expired_unpinned",
                    {"message_id": row["message_id"], "pin_hours": pin_hours},
                )
            count += 1
        for drow in delete_rows:
            try:
                if drow["message_id"] and hasattr(self.gateway, "delete_message"):
                    self.gateway.delete_message(
                        chat_id=drow["telegram_chat_id"],
                        message_id=drow["message_id"],
                    )
            except Exception as exc:
                logger.warning("delete_message_failed delivery=%s error=%s", drow["id"], exc)
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE deliveries SET delete_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (drow["id"],),
                )
                self._snapshot(
                    conn,
                    drow["order_id"],
                    drow["id"],
                    "scheduled_delete_executed",
                    {"message_id": drow["message_id"]},
                )
            count += 1
        return count

    def sync_delivery_metrics(
        self,
        checkpoint_label: str = "24h",
        views_by_delivery: dict[str, int] | None = None,
        abc_client: TelegramABCClient | None = None,
    ) -> int:
        """
        Sample post views/forwards at 1h/6h/24h/48h checkpoints via TelegramABC
        (or explicit views_by_delivery map) and update deliveries.impression_count.
        """
        client = abc_client or TelegramABCClient.from_env()
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.message_id, d.impression_count, c.telegram_chat_id
                FROM deliveries d
                JOIN channels c ON c.id = d.channel_id
                WHERE d.status IN ('sent', 'confirmed')
                  AND d.message_id IS NOT NULL
                ORDER BY d.sent_at DESC
                LIMIT 200
                """
            ).fetchall()

        updated_count = 0
        for row in rows:
            delivery_id = str(row["id"])
            views_val: int | None = None
            forwards_val = 0
            if views_by_delivery and delivery_id in views_by_delivery:
                views_val = max(0, int(views_by_delivery[delivery_id]))
            elif client.is_configured and row["message_id"]:
                fetched = client.fetch_message_views(
                    telegram_chat_id=row["telegram_chat_id"],
                    message_id=row["message_id"],
                )
                if fetched is not None:
                    views_val = fetched["views"]
                    forwards_val = fetched["forwards"]

            if views_val is None:
                continue

            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO delivery_metric_snapshots (
                        id, delivery_id, checkpoint_label, views_count, forwards_count
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (new_id("dms"), delivery_id, checkpoint_label, views_val, forwards_val),
                )
                conn.execute(
                    """
                    UPDATE deliveries
                    SET impression_count = MAX(impression_count, ?),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (views_val, delivery_id),
                )
            updated_count += 1
        return updated_count

    def reconcile_delivery(
        self,
        delivery_id: str,
        *,
        resolution: str,
        actor_account_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """
        Manually reconcile a `reconciliation_required` delivery:
        - `confirm_sent`: settle ledger (`charge_delivery`) and mark delivery `'sent'`.
        - `confirm_failed`: mark delivery `'failed'` and release order reserved budget.
        """
        if resolution not in {"confirm_sent", "confirm_failed"}:
            raise InvalidState(f"不支持的对账结论：{resolution}")
        with self.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            if not delivery:
                raise NotFound(f"投放记录不存在：{delivery_id}")
            if delivery["status"] != "reconciliation_required":
                raise InvalidState("仅可对 reconciliation_required 状态的投放记录执行对账处理")
            order = self.orders.get_order(conn, delivery["order_id"])
            channel = conn.execute("SELECT * FROM channels WHERE id = ?", (delivery["channel_id"],)).fetchone()
            config = conn.execute("SELECT * FROM channel_configs WHERE channel_id = ?", (delivery["channel_id"],)).fetchone()
            if not channel or not config:
                raise InvalidState("投放关联的频道或配置不存在")
            if resolution == "confirm_sent":
                publisher_account_id = order.get("publisher_account_id") or channel["owner_account_id"]
                publisher_net, fee = self.ledger.charge_delivery(
                    conn,
                    advertiser_account_id=order["advertiser_account_id"],
                    publisher_account_id=publisher_account_id,
                    order_id=order["id"],
                    delivery_id=delivery["id"],
                    gross_cents=order["unit_price_cents"],
                    service_fee_bps=config["service_fee_bps"],
                    currency=order["currency"],
                )
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'sent',
                        sent_at = COALESCE(sent_at, CURRENT_TIMESTAMP),
                        charge_cents = ?,
                        publisher_net_cents = ?,
                        platform_fee_cents = ?,
                        error_message = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (order["unit_price_cents"], publisher_net, fee, delivery["id"]),
                )
                conn.execute(
                    """
                    UPDATE ad_orders
                    SET status = 'running',
                        spent_cents = spent_cents + ?,
                        reserved_cents = reserved_cents - ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (order["unit_price_cents"], order["unit_price_cents"], order["id"]),
                )
                self._snapshot(
                    conn,
                    order["id"],
                    delivery["id"],
                    "reconciled_sent",
                    {"actor_account_id": actor_account_id, "note": note},
                )
                self.orders.maybe_schedule_next(conn, order["id"])
            else:
                self._mark_delivery_failed(
                    conn,
                    delivery,
                    order,
                    note or "运营人工对账确认未发帖，已释放冻结预算",
                )
                self._snapshot(
                    conn,
                    order["id"],
                    delivery["id"],
                    "reconciled_failed",
                    {"actor_account_id": actor_account_id, "note": note},
                )
            updated = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            return dict(updated)

    def _dispatch_one_isolated(self, delivery_id: str) -> dict[str, Any] | None:
        # Stage 1: Claim delivery ('scheduled' -> 'sending') and load context in a short transaction.
        with self.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            if not delivery or delivery["status"] != "scheduled":
                return None
            order = self.orders.get_order(conn, delivery["order_id"])
            if order["status"] not in {"approved", "running"}:
                return None
            channel = conn.execute("SELECT * FROM channels WHERE id = ?", (delivery["channel_id"],)).fetchone()
            creative = conn.execute("SELECT * FROM creatives WHERE id = ?", (delivery["creative_id"],)).fetchone()
            config = conn.execute("SELECT * FROM channel_configs WHERE channel_id = ?", (delivery["channel_id"],)).fetchone()
            slot = conn.execute("SELECT * FROM ad_slots WHERE id = ?", (order["slot_id"],)).fetchone()
            if not channel or not creative or not config or not slot:
                raise RuntimeError("delivery references missing channel/creative/config/slot")
            if order["reserved_cents"] < order["unit_price_cents"]:
                self.orders.maybe_schedule_next(conn, order["id"])
                return {"delivery_id": delivery["id"], "status": "budget_exhausted"}
            cur = conn.execute(
                "UPDATE deliveries SET status = 'sending', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'scheduled'",
                (delivery_id,),
            )
            if cur.rowcount != 1:
                return None

        # Stage 2: External Telegram I/O outside any SQLite transaction lock.
        track_url = f"https://t.me/{self.settings.bot_username}?start=ad_{delivery['id']}"
        sales_url = f"https://t.me/{self.settings.bot_username}?start=ch_{channel['ref_token']}"
        ad_text = creative["text"]
        message_id: str
        if slot["slot_type"] in {"light_tail", "button_tail"}:
            button_text = (
                creative["button_text"]
                or creative["light_short_text"]
                or creative["short_text"]
                or "查看详情"
            ).strip() or "查看详情"
            with self.db.transaction() as conn:
                inserted_message_id = self._insert_tail_into_latest_post(
                    conn,
                    channel,
                    button_text,
                    track_url,
                )
            if inserted_message_id:
                message_id = inserted_message_id
            else:
                ad_text = f"🔗 {button_text}"
                try:
                    message_id = self.gateway.send_ad(
                        chat_id=channel["telegram_chat_id"],
                        text=ad_text,
                        inline_keyboard=[[{"text": button_text, "url": track_url}]],
                    )
                except TelegramError as exc:
                    with self.db.transaction() as conn:
                        self._mark_delivery_failed(conn, delivery, order, str(exc))
                    return {"delivery_id": delivery["id"], "status": "failed", "error": str(exc)}
        else:
            keyboard = self._build_post_keyboard(creative, sales_url=sales_url, track_url=track_url)
            button_text = (creative["button_text"] or "").strip() or "查看详情"
            if slot["slot_type"] in {"standard_card", "pin24h"} and creative["standard_text"]:
                ad_text = creative["standard_text"]
            try:
                if creative["media_file_id"] and slot["slot_type"] in {"standard_card", "pin24h"}:
                    message_id = self.gateway.send_media_ad(
                        chat_id=channel["telegram_chat_id"],
                        media_file_id=creative["media_file_id"],
                        media_type=creative["media_type"] or "photo",
                        caption=ad_text,
                        button_text=button_text,
                        button_url=track_url,
                        inline_keyboard=keyboard,
                    )
                else:
                    message_id = self.gateway.send_ad(
                        chat_id=channel["telegram_chat_id"],
                        text=ad_text,
                        inline_keyboard=keyboard,
                    )
            except TelegramError as exc:
                with self.db.transaction() as conn:
                    self._mark_delivery_failed(conn, delivery, order, str(exc))
                return {"delivery_id": delivery["id"], "status": "failed", "error": str(exc)}

        # Immediately persist message_id so it is never lost if pinning or settlement fails.
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE deliveries SET message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(message_id), delivery["id"]),
            )

        pinned = 0
        if slot["slot_type"] == "pin24h":
            try:
                self.gateway.pin_message(chat_id=channel["telegram_chat_id"], message_id=message_id)
                pinned = 1
            except TelegramError as exc:
                if hasattr(self.gateway, "delete_message"):
                    try:
                        self.gateway.delete_message(chat_id=channel["telegram_chat_id"], message_id=message_id)
                    except Exception:
                        pass
                with self.db.transaction() as conn:
                    self._mark_delivery_failed(conn, delivery, order, str(exc), message_id=str(message_id))
                return {"delivery_id": delivery["id"], "status": "failed", "error": str(exc)}

        # Stage 3: Short transaction for atomic ledger settlement + state transition.
        publisher_account_id = order.get("publisher_account_id") or channel["owner_account_id"]
        try:
            with self.db.transaction() as conn:
                publisher_net, fee = self.ledger.charge_delivery(
                    conn,
                    advertiser_account_id=order["advertiser_account_id"],
                    publisher_account_id=publisher_account_id,
                    order_id=order["id"],
                    delivery_id=delivery["id"],
                    gross_cents=order["unit_price_cents"],
                    service_fee_bps=config["service_fee_bps"],
                    currency=order["currency"],
                )
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'sent',
                        sent_at = CURRENT_TIMESTAMP,
                        message_id = ?,
                        pinned = ?,
                        unpin_at = CASE WHEN ? = 1 THEN datetime('now', '+24 hours') ELSE unpin_at END,
                        charge_cents = ?,
                        publisher_net_cents = ?,
                        platform_fee_cents = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (message_id, pinned, pinned, order["unit_price_cents"], publisher_net, fee, delivery["id"]),
                )
                conn.execute(
                    """
                    UPDATE ad_orders
                    SET status = 'running',
                        spent_cents = spent_cents + ?,
                        reserved_cents = reserved_cents - ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (order["unit_price_cents"], order["unit_price_cents"], order["id"]),
                )
                self._snapshot(conn, order["id"], delivery["id"], "send_log", {"message_id": message_id, "pinned": bool(pinned)})
                self._snapshot(conn, order["id"], delivery["id"], "channel_config", dict(config))
                self._notify_advertiser_delivered(
                    conn,
                    order=order,
                    channel=channel,
                    delivery_id=delivery["id"],
                    charge_cents=order["unit_price_cents"],
                )
                self._notify_publisher_income(conn, channel, creative, message_id, publisher_net)
                self._maybe_notify_budget(conn, order["id"])
                self.orders.maybe_schedule_next(conn, order["id"])
        except Exception as exc:
            logger.error("settlement_failed_after_send delivery=%s message_id=%s err=%s", delivery["id"], message_id, exc)
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'reconciliation_required',
                        message_id = ?,
                        error_message = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (str(message_id), f"sent_but_settlement_failed: {exc}", delivery["id"]),
                )
            return {
                "delivery_id": delivery["id"],
                "status": "reconciliation_required",
                "message_id": message_id,
                "error": str(exc),
            }
        return {"delivery_id": delivery["id"], "status": "sent", "message_id": message_id}

    def _build_post_keyboard(
        self,
        creative: sqlite3.Row,
        *,
        sales_url: str,
        track_url: str,
    ) -> list[list[dict[str, str]]]:
        cta_text = (creative["button_text"] or "").strip() or "查看详情"
        target_url = creative["target_url"]
        return [
            [
                {"text": "📣 频道招商", "url": sales_url},
                {"text": "🔍 查看详情", "url": track_url},
            ],
            [{"text": cta_text, "url": target_url}],
        ]

    def _insert_tail_into_latest_post(
        self,
        conn: sqlite3.Connection,
        channel: sqlite3.Row,
        button_text: str,
        track_url: str,
    ) -> str | None:
        row = conn.execute("SELECT value FROM runtime_state WHERE key = ?", (f"channel_latest_post:{channel['id']}",)).fetchone()
        if not row:
            return None
        try:
            latest = json.loads(row["value"])
        except json.JSONDecodeError:
            return None
        original_text = str(latest.get("text") or "").strip()
        if not original_text:
            return None
        updated_text = original_text
        keyboard = latest.get("inline_keyboard") or []
        detail_button = {"text": button_text, "url": track_url}
        if not any(button.get("url") == track_url for row_buttons in keyboard for button in row_buttons):
            keyboard.append([detail_button])
        try:
            self.gateway.edit_channel_message_text(
                chat_id=latest["chat_id"],
                message_id=latest["message_id"],
                text=updated_text,
                inline_keyboard=keyboard,
            )
        except TelegramError:
            return None
        conn.execute(
            """
            INSERT INTO runtime_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (
                f"channel_latest_post:{channel['id']}",
                json.dumps(
                    {
                        **latest,
                        "text": updated_text,
                        "inline_keyboard": keyboard,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return str(latest["message_id"])

    def _mark_delivery_failed(
        self,
        conn: sqlite3.Connection,
        delivery: sqlite3.Row,
        order: dict[str, Any],
        error: str,
        *,
        message_id: str | None = None,
    ) -> None:
        conn.execute(
            """
            UPDATE deliveries
            SET status = 'failed',
                message_id = COALESCE(?, message_id),
                error_message = ?,
                retry_count = retry_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message_id, error, delivery["id"]),
        )
        failure_payload: dict[str, Any] = {"error": error}
        if message_id is not None:
            failure_payload["message_id"] = message_id
        self._snapshot(conn, order["id"], delivery["id"], "send_failure", failure_payload)
        self.orders.pause_and_release(conn, order["id"], f"插播发布失败：{error}")

    def _notify_advertiser_delivered(
        self,
        conn: sqlite3.Connection,
        *,
        order: dict[str, Any],
        channel: sqlite3.Row,
        delivery_id: str,
        charge_cents: int,
    ) -> None:
        """Push '✅ 已发布' to the advertiser after a successful send.

        Failure to reach Telegram does not affect the delivery transaction —
        we log and continue. The advertiser can still find the same info
        under '📣 我的广告'.
        """
        account = conn.execute(
            "SELECT telegram_user_id FROM accounts WHERE id = ?",
            (order["advertiser_account_id"],),
        ).fetchone()
        if not account or not account["telegram_user_id"]:
            return
        track_url = f"https://t.me/{self.settings.bot_username}?start=ad_{delivery_id}"
        remaining_cents = max(0, int(order["reserved_cents"]) - int(charge_cents))
        text = (
            "✅ 你的广告已发布\n\n"
            f"📺 {channel['title']}\n"
            f"💰 本次扣费 USD {cents_to_money(charge_cents)}\n"
            f"💵 订单剩余预算 USD {cents_to_money(remaining_cents)}"
        )
        keyboard = [
            [{"text": "🔍 查看详情", "url": track_url}],
            [{"text": "📣 我的广告", "callback_data": "advertiser:orders"}],
        ]
        try:
            self.gateway.send_private_message(
                chat_id=account["telegram_user_id"],
                text=text,
                inline_keyboard=keyboard,
            )
        except Exception as exc:
            logger.warning(
                "notify_advertiser_delivered_failed order=%s delivery=%s error=%s",
                order["id"],
                delivery_id,
                exc,
            )

    def _maybe_notify_budget(self, conn: sqlite3.Connection, order_id: str) -> None:
        order = self.orders.get_order(conn, order_id)
        if order["budget_cents"] <= 0 or order["low_budget_notified_at"]:
            return
        remaining = order["reserved_cents"]
        if remaining * 100 <= order["budget_cents"] * 20:
            account = conn.execute(
                "SELECT * FROM accounts WHERE id = ?",
                (order["advertiser_account_id"],),
            ).fetchone()
            if account and account["telegram_user_id"]:
                try:
                    self.gateway.send_private_message(
                        chat_id=account["telegram_user_id"],
                        text=f"你的插播预算已低于 20%，订单 {order_id} 剩余预算即将用完。",
                    )
                except Exception as exc:
                    logger.warning(
                        "notify_low_budget_failed order=%s error=%s",
                        order_id,
                        exc,
                    )
            self.orders.mark_low_budget_notified(conn, order_id)

    def _notify_publisher_income(
        self,
        conn: sqlite3.Connection,
        channel: sqlite3.Row,
        creative: sqlite3.Row,
        message_id: str,
        publisher_net_cents: int,
    ) -> None:
        if publisher_net_cents <= 0:
            return
        recipients = self._publisher_income_notification_recipients(conn, channel)
        if not recipients:
            return
        post_url = self._channel_post_url(channel, message_id)
        channel_name = html.escape(str(channel["title"]))
        channel_label = f'<a href="{html.escape(post_url)}">{channel_name}</a>' if post_url else f"<b>{channel_name}</b>"
        campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (creative["campaign_id"],)).fetchone()
        ad_label = self._short_text(str((campaign["name"] if campaign else None) or creative["text"] or "广告"), 22)
        keyboard = [
            [
                {"text": "🔕 关闭通知", "callback_data": "publisher:disable_income_notifications"},
                {"text": "💰 我的钱包", "callback_data": "publisher:earnings"},
            ]
        ]
        for account in recipients:
            total_cents = (
                int(account["pending_earnings_cents"] or 0)
                + int(account["confirmed_earnings_cents"] or 0)
            )
            text = (
                f"<b>💸 收入到账 +USD {self._money(publisher_net_cents)}</b>\n\n"
                f"频道：{channel_label}\n"
                f"广告：{html.escape(ad_label)}\n"
                f"当前收益：USD {self._money(total_cents)}"
            )
            try:
                self.gateway.send_private_message(
                    chat_id=account["telegram_user_id"],
                    text=text,
                    inline_keyboard=keyboard,
                    parse_mode="HTML",
                )
            except TelegramError as exc:
                self._snapshot(
                    conn,
                    None,
                    None,
                    "publisher_income_notification_failed",
                    {"channel_id": channel["id"], "account_id": account["id"], "error": str(exc)},
                )

    def _publisher_income_notification_recipients(self, conn: sqlite3.Connection, channel: sqlite3.Row) -> list[sqlite3.Row]:
        rows = conn.execute(
            """
            SELECT a.*
            FROM accounts a
            WHERE a.id = ?
              AND a.telegram_user_id IS NOT NULL
              AND a.publisher_income_notifications_enabled = 1
            UNION
            SELECT a.*
            FROM channel_admins ca
            JOIN accounts a ON a.telegram_user_id = ca.telegram_user_id
            WHERE ca.channel_id = ?
              AND ca.status IN ('creator', 'administrator')
              AND ca.is_bot = 0
              AND a.telegram_user_id IS NOT NULL
              AND a.publisher_income_notifications_enabled = 1
            """,
            (channel["owner_account_id"], channel["id"]),
        ).fetchall()
        unique: dict[str, sqlite3.Row] = {}
        for row in rows:
            unique[str(row["telegram_user_id"])] = row
        return list(unique.values())

    def _channel_post_url(self, channel: sqlite3.Row, message_id: str) -> str | None:
        username = str(channel["username"] or "").strip().lstrip("@")
        if username:
            return f"https://t.me/{username}/{message_id}"
        chat_id = str(channel["telegram_chat_id"] or "")
        if chat_id.startswith("-100"):
            return f"https://t.me/c/{chat_id[4:]}/{message_id}"
        return None

    def _short_text(self, text: str, limit: int) -> str:
        text = " ".join((text or "").strip().split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    def _money(self, amount_cents: int) -> str:
        return f"{amount_cents / 100:.2f}"

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
