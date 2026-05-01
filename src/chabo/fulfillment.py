from __future__ import annotations

import json
import sqlite3
import html
from typing import Any

from .config import Settings
from .db import Database
from .ids import new_id
from .services import ChannelService, LedgerService, OrderService, iso, utcnow
from .telegram import MessageGateway, TelegramError


class FulfillmentService:
    def __init__(self, db: Database, settings: Settings, gateway: MessageGateway):
        self.db = db
        self.settings = settings
        self.gateway = gateway
        self.channels = ChannelService(db, settings)
        self.ledger = LedgerService(db, settings)
        self.orders = OrderService(db, settings)

    def dispatch_due(self, limit: int = 20) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        now = iso()
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT d.*
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
            for delivery in rows:
                sent.append(self._dispatch_one(conn, delivery))
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

    def _dispatch_one(self, conn: sqlite3.Connection, delivery: sqlite3.Row) -> dict[str, Any]:
        order = self.orders.get_order(conn, delivery["order_id"])
        channel = conn.execute("SELECT * FROM channels WHERE id = ?", (delivery["channel_id"],)).fetchone()
        creative = conn.execute("SELECT * FROM creatives WHERE id = ?", (delivery["creative_id"],)).fetchone()
        config = conn.execute("SELECT * FROM channel_configs WHERE channel_id = ?", (delivery["channel_id"],)).fetchone()
        slot = conn.execute("SELECT * FROM ad_slots WHERE id = ?", (order["slot_id"],)).fetchone()
        if not channel or not creative or not config or not slot:
            raise RuntimeError("delivery references missing channel/creative/config/slot")
        if order["reserved_cents"] < order["unit_price_cents"]:
            self.orders.maybe_schedule_next(conn, order["id"])
            return {"delivery_id": delivery["id"], "status": "budget_exhausted"}

        track_url = f"https://t.me/{self.settings.bot_username}?start=ad_{delivery['id']}"
        ad_text = creative["text"]
        button_text = creative["button_text"]
        message_id: str
        if slot["slot_type"] in {"light_tail", "button_tail"}:
            short_text = creative["short_text"] or creative["button_text"] or "查看详情"
            inserted_message_id = self._insert_tail_into_latest_post(
                conn,
                channel,
                short_text if slot["slot_type"] == "light_tail" else None,
                "查看完整广告" if slot["slot_type"] == "light_tail" else creative["button_text"] or "查看详情",
                track_url,
            )
            if inserted_message_id:
                message_id = inserted_message_id
            else:
                ad_text = f"🔖 {short_text}" if slot["slot_type"] == "light_tail" else f"🔗 {creative['button_text'] or '查看详情'}"
                button_text = "查看完整广告"
                try:
                    message_id = self.gateway.send_ad(
                        chat_id=channel["telegram_chat_id"],
                        text=ad_text,
                        button_text=button_text,
                        button_url=track_url,
                    )
                except TelegramError as exc:
                    self._mark_delivery_failed(conn, delivery, order, str(exc))
                    return {"delivery_id": delivery["id"], "status": "failed", "error": str(exc)}
        else:
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
                    )
                else:
                    message_id = self.gateway.send_ad(
                        chat_id=channel["telegram_chat_id"],
                        text=ad_text,
                        button_text=button_text,
                        button_url=track_url,
                    )
            except TelegramError as exc:
                self._mark_delivery_failed(conn, delivery, order, str(exc))
                return {"delivery_id": delivery["id"], "status": "failed", "error": str(exc)}
        pinned = 0
        if slot["slot_type"] == "pin24h":
            try:
                self.gateway.pin_message(chat_id=channel["telegram_chat_id"], message_id=message_id)
                pinned = 1
            except TelegramError as exc:
                self._mark_delivery_failed(conn, delivery, order, str(exc))
                return {"delivery_id": delivery["id"], "status": "failed", "error": str(exc)}

        publisher_net, fee = self.ledger.charge_delivery(
            conn,
            advertiser_account_id=order["advertiser_account_id"],
            publisher_account_id=channel["owner_account_id"],
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
                charge_cents = ?,
                publisher_net_cents = ?,
                platform_fee_cents = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message_id, pinned, order["unit_price_cents"], publisher_net, fee, delivery["id"]),
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
        self._notify_publisher_income(conn, channel, creative, message_id, publisher_net)
        self._maybe_notify_budget(conn, order["id"])
        self.orders.maybe_schedule_next(conn, order["id"])
        return {"delivery_id": delivery["id"], "status": "sent", "message_id": message_id}

    def _insert_tail_into_latest_post(
        self,
        conn: sqlite3.Connection,
        channel: sqlite3.Row,
        short_text: str | None,
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
        insertion = f"🔖 {short_text}" if short_text else ""
        updated_text = original_text if not insertion or insertion in original_text else f"{original_text}\n\n{insertion}"
        if len(updated_text) > 3900:
            return None
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
    ) -> None:
        conn.execute(
            """
            UPDATE deliveries
            SET status = 'failed',
                error_message = ?,
                retry_count = retry_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, delivery["id"]),
        )
        self._snapshot(conn, order["id"], delivery["id"], "send_failure", {"error": error})
        self.orders.pause_and_release(conn, order["id"], f"插播发布失败：{error}")

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
                self.gateway.send_private_message(
                    chat_id=account["telegram_user_id"],
                text=f"你的插播预算已低于 20%，订单 {order_id} 剩余预算即将用完。",
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
