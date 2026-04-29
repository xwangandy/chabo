from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database
from .ids import new_id
from .money import cents_to_money, money_to_cents
from .services import AccountService, AdvertiserService, AdvertiserSubscriptionService, ChaboError, ChannelService, DisputeService, InsufficientBalance, InvalidState, LedgerService, LightProbeService, MaterialService, NotFound, OrderService, SelfPromoService, StarsPaymentService
from .telegram import MessageGateway, TelegramError
from .timezones import DEFAULT_USER_TIMEZONE, TIMEZONE_ALIASES, format_timezone_now, resolve_timezone


SLOT_DISPLAY_NAMES = {
    "light_tail": "文字插播",
    "standard": "标准插播",
    "standard_card": "标准插播",
    "strong_post": "定制插播",
    "pin24h": "置顶 24h",
    "loop_daily": "循环发布",
}

SLOT_EMOJIS = {
    "light_tail": "✍️",
    "standard": "🧾",
    "standard_card": "🧾",
    "strong_post": "🎨",
    "pin24h": "📌",
    "loop_daily": "🔁",
}

PLACEMENT_SLOT_TYPES = ("light_tail", "standard_card", "strong_post")
PINNABLE_PLACEMENT_SLOTS = {"standard_card", "strong_post"}
PLACEMENT_PERIODS = {
    "once": {"label": "发布一次", "deliveries": 1, "discount_bps": 10000},
    "week": {"label": "7 天循环", "deliveries": 7, "discount_bps": 9000},
    "month": {"label": "30 天循环", "deliveries": 30, "discount_bps": 8000},
    "monthly": {"label": "连续包月", "deliveries": 30, "discount_bps": 7000},
}


class UpdateHandler:
    def __init__(self, db: Database, settings: Settings, gateway: MessageGateway):
        self.db = db
        self.settings = settings
        self.gateway = gateway
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings, gateway=gateway)
        self.ledger = LedgerService(db, settings)
        self.light_probes = LightProbeService(db, settings)
        self.materials = MaterialService(db, settings)
        self.orders = OrderService(db, settings, gateway=gateway)
        self.self_promos = SelfPromoService(db, settings)
        self.stars_payments = StarsPaymentService(db, settings)
        self.advertisers = AdvertiserService(db, settings)
        self.advertiser_subscriptions = AdvertiserSubscriptionService(db, settings)
        self.disputes = DisputeService(db, settings)

    def handle(self, update: dict[str, Any]) -> dict[str, Any]:
        if "pre_checkout_query" in update:
            return self._handle_pre_checkout_query(update["pre_checkout_query"])
        if "callback_query" in update:
            return self._handle_callback_query(update["callback_query"])
        if "my_chat_member" in update:
            return self._handle_my_chat_member(update["my_chat_member"])
        if "chat_member" in update:
            return self._handle_chat_member(update["chat_member"])
        if "message" in update:
            return self._handle_message(update["message"])
        if "channel_post" in update:
            return self._handle_channel_post(update["channel_post"])
        return {"handled": False, "reason": "unsupported_update"}

    def _handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        if "successful_payment" in message:
            return self._handle_successful_payment(message)
        text = message.get("text") or ""
        if text.startswith("/cancel"):
            return self._handle_cancel(message)
        if text.startswith("/menu"):
            return self._handle_menu(message)
        if text.startswith("/start"):
            payload = text.removeprefix("/start").strip()
            return self._handle_start(message, payload)
        conversation_result = self._handle_conversation_message(message, text)
        if conversation_result["handled"]:
            return conversation_result
        return {"handled": False, "reason": "unsupported_message"}

    def _handle_cancel(self, message: dict[str, Any]) -> dict[str, Any]:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id:
            self._clear_conversation(chat_id)
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="已取消当前插播操作。",
                inline_keyboard=[[{"text": "返回主菜单", "callback_data": "menu:home"}]],
            )
        return {"handled": True, "type": "conversation_cancelled"}

    def _handle_menu(self, message: dict[str, Any]) -> dict[str, Any]:
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = user.get("id") or chat.get("id")
        if not user_id:
            return {"handled": False, "reason": "missing_user"}
        display_name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part) or user.get("username")
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", display_name)
            if not account["timezone_confirmed_at"]:
                self._prompt_timezone(chat.get("id", user_id), account["id"], "", None, conn=conn)
                return {"handled": True, "type": "timezone_prompt"}
        self._send_main_menu(chat.get("id", user_id), user=user)
        return {"handled": True, "type": "main_menu"}

    def _handle_start(self, message: dict[str, Any], payload: str) -> dict[str, Any]:
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = user.get("id") or chat.get("id")
        if not user_id:
            return {"handled": False, "reason": "missing_user"}
        display_name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part) or user.get("username")
        channel_for_landing: dict[str, Any] | None = None
        channel_start_result: dict[str, Any] | None = None
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", display_name)
            if not account["timezone_confirmed_at"]:
                self._prompt_timezone(chat.get("id", user_id), account["id"], payload, None, conn=conn)
                return {"handled": True, "type": "timezone_prompt", "pending_start_payload": payload}
            if payload.startswith("ad_"):
                delivery_id = payload.removeprefix("ad_")
                delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
                if delivery:
                    conn.execute(
                        """
                        INSERT INTO metric_snapshots (id, delivery_id, metric_type, value, metadata_json)
                        VALUES (?, ?, 'bot_start', 1, ?)
                        """,
                        (new_id("met"), delivery_id, json.dumps({"telegram_user_id": str(user_id)}, ensure_ascii=False)),
                    )
                    creative = conn.execute("SELECT * FROM creatives WHERE id = ?", (delivery["creative_id"],)).fetchone()
                    source_channel = conn.execute(
                        "SELECT * FROM channels WHERE id = ?", (delivery["channel_id"],)
                    ).fetchone()
                    if creative and source_channel:
                        text, keyboard = self._build_ad_detail_view(creative, source_channel)
                        self.gateway.send_private_message(
                            chat_id=chat.get("id", user_id),
                            text=text,
                            inline_keyboard=keyboard,
                        )
                    else:
                        self.gateway.send_private_message(chat_id=chat.get("id", user_id), text="广告详情暂不可用")
                    return {"handled": True, "type": "ad_start", "delivery_id": delivery_id}
            if payload.startswith("sp_"):
                self_promo_id = payload.removeprefix("sp_")
                row = conn.execute(
                    "SELECT * FROM self_promo_publishes WHERE id = ?", (self_promo_id,)
                ).fetchone()
                if row:
                    creative = conn.execute(
                        "SELECT * FROM creatives WHERE id = ?", (row["creative_id"],)
                    ).fetchone()
                    source_channel = conn.execute(
                        "SELECT * FROM channels WHERE id = ?", (row["channel_id"],)
                    ).fetchone()
                    if creative and source_channel:
                        text, keyboard = self._build_ad_detail_view(creative, source_channel)
                        self.gateway.send_private_message(
                            chat_id=chat.get("id", user_id),
                            text=text,
                            inline_keyboard=keyboard,
                        )
                    else:
                        self.gateway.send_private_message(chat_id=chat.get("id", user_id), text="广告详情暂不可用")
                    return {"handled": True, "type": "self_promo_start", "self_promo_id": self_promo_id}
            if payload.startswith("probe_"):
                probe_id = payload.removeprefix("probe_")
                probe = conn.execute("SELECT * FROM light_probes WHERE id = ?", (probe_id,)).fetchone()
                if probe:
                    self.light_probes.record_click(
                        conn,
                        probe_id=probe_id,
                        telegram_user_id=user_id,
                        metadata={"chat_id": str(chat.get("id", user_id))},
                    )
                    self.gateway.send_private_message(
                        chat_id=chat.get("id", user_id),
                        text=f"{probe['detail_text']}\n\n{probe['target_url']}",
                    )
                    return {"handled": True, "type": "light_probe_start", "probe_id": probe_id, "channel_id": probe["channel_id"]}
            channel = None
            for channel_token in self._channel_tokens_from_start_payload(payload):
                channel = self.channels.get_by_token(conn, channel_token)
                if channel:
                    break
            if channel:
                channel = self._sync_channel_profile_conn(conn, channel)
            session_id = new_id("sess")
            conn.execute(
                """
                INSERT INTO advertiser_sessions (id, advertiser_account_id, ref_channel_id, start_payload)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, account["id"], channel["id"] if channel else None, payload or "organic"),
            )
            if channel:
                channel_for_landing = dict(channel)
                channel_start_result = {"handled": True, "type": "channel_start", "channel_id": channel["id"], "session_id": session_id}
            else:
                organic_session_id = session_id
        if channel_for_landing and channel_start_result:
            self._send_channel_sales_landing(chat.get("id", user_id), channel_for_landing, [])
            return channel_start_result
        publisher_channels = self._publisher_channels_for_user(user_id, display_name)
        if publisher_channels:
            self._send_publisher_menu(chat.get("id", user_id), user, channels=publisher_channels)
        else:
            self._send_main_menu(chat.get("id", user_id), user=user)
        return {"handled": True, "type": "organic_start", "session_id": organic_session_id}

    def _handle_callback_query(self, query: dict[str, Any]) -> dict[str, Any]:
        data = query.get("data") or ""
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user = query.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        if not chat_id:
            return {"handled": False, "reason": "missing_callback_chat"}
        try:
            self.gateway.answer_callback_query(callback_query_id=query["id"], text="处理中...")
        except TelegramError:
            pass

        if data == "menu:home":
            self._send_main_menu(chat_id, message, user)
            return {"handled": True, "type": "callback_main_menu"}
        if data == "timezone:yes":
            pending_payload = self._confirm_timezone(chat_id, user, DEFAULT_USER_TIMEZONE)
            self._continue_after_timezone(chat_id, user, pending_payload, message)
            return {"handled": True, "type": "callback_timezone_confirmed", "timezone": DEFAULT_USER_TIMEZONE}
        if data in {"timezone:no", "timezone:change"}:
            account = self._ensure_mixed_account(user, chat_id)
            self._set_conversation(chat_id, account["id"], "timezone_setup", "input", {"pending_start_payload": ""})
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text=(
                    "🌐 设置时区\n\n"
                    "请输入城市或时区。\n"
                    "例如：北京、上海、Manila、Asia/Tokyo、Europe/Rome"
                ),
                inline_keyboard=[[{"text": "🏠 主菜单", "callback_data": "menu:home"}]],
            )
            return {"handled": True, "type": "callback_timezone_input_requested"}
        if data == "role:advertiser":
            self._send_advertiser_menu(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_menu"}
        if data == "role:publisher":
            self._send_publisher_menu(chat_id, user, message)
            return {"handled": True, "type": "callback_publisher_menu"}
        if data == "publisher:channels":
            self._send_publisher_menu(chat_id, user, message)
            return {"handled": True, "type": "callback_publisher_channels"}
        if data == "publisher:earnings":
            self._send_publisher_earnings(chat_id, user, message)
            return {"handled": True, "type": "callback_publisher_earnings"}
        if data == "earnings:channels":
            self._send_earnings_channels(chat_id, user, message)
            return {"handled": True, "type": "callback_earnings_channels"}
        if data == "earnings:statement":
            self._send_earnings_statement(chat_id, user, message)
            return {"handled": True, "type": "callback_earnings_statement"}
        if data == "advertiser:balance":
            self._send_advertiser_balance(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_balance"}
        if data == "wallet:topup":
            self._send_wallet_topup_picker(chat_id, user, message)
            return {"handled": True, "type": "callback_wallet_topup_picker"}
        if data.startswith("wallet:topup:"):
            stars_str = data.removeprefix("wallet:topup:")
            try:
                stars_amount = int(stars_str)
            except ValueError:
                self._send_wallet_topup_picker(chat_id, user, message)
                return {"handled": True, "type": "callback_wallet_topup_invalid"}
            return self._trigger_wallet_topup(chat_id, user, stars_amount, message)
        if data == "wallet:reserved":
            self._send_wallet_reserved(chat_id, user, message)
            return {"handled": True, "type": "callback_wallet_reserved"}
        if data == "wallet:statement":
            self._send_wallet_statement(chat_id, user, message)
            return {"handled": True, "type": "callback_wallet_statement"}
        if data == "advertiser:library":
            self._send_advertiser_library(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_library"}
        if data == "advertiser:discover":
            self._send_advertiser_discover(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_discover"}
        if data == "advertiser:saved":
            self._send_advertiser_saved(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_saved"}
        if data == "advertiser:alerts":
            self._send_advertiser_alerts(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_alerts"}
        if data == "advertiser:plan":
            self._send_advertiser_plan_panel(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_plan"}
        if data.startswith("advertiser:plan:buy:"):
            plan = data.removeprefix("advertiser:plan:buy:")
            return self._trigger_advertiser_plan_purchase(chat_id, user, plan, message)
        if data.startswith("advertiser:save:"):
            tail = data.removeprefix("advertiser:save:")
            origin, _, channel_id = tail.partition(":")
            if not channel_id:
                # No origin prefix → fall back to library origin
                channel_id, origin = origin, "library"
            self._toggle_saved_channel(chat_id, user, channel_id, origin, message)
            return {"handled": True, "type": "callback_advertiser_save_toggle", "channel_id": channel_id, "origin": origin}
        if data == "advertiser:material:new":
            self._send_material_format_picker(chat_id, user, message)
            return {"handled": True, "type": "callback_material_new_picker"}
        if data.startswith("advertiser:material:new:"):
            format_type = data.removeprefix("advertiser:material:new:")
            self._begin_material_create(chat_id, user, format_type, message)
            return {"handled": True, "type": "callback_material_new_started", "format_type": format_type}
        if data.startswith("advertiser:material:edit:"):
            material_id = data.removeprefix("advertiser:material:edit:")
            self._send_material_edit_panel(chat_id, user, material_id, message)
            return {"handled": True, "type": "callback_material_edit_panel", "material_id": material_id}
        if data.startswith("advertiser:material:field:"):
            tail = data.removeprefix("advertiser:material:field:")
            material_id, _, field = tail.partition(":")
            self._begin_material_field_edit(chat_id, user, material_id, field, message)
            return {"handled": True, "type": "callback_material_edit_field", "material_id": material_id, "field": field}
        if data.startswith("advertiser:batch:start:"):
            material_id = data.removeprefix("advertiser:batch:start:")
            self._begin_batch_orders(chat_id, user, material_id, message)
            return {"handled": True, "type": "callback_batch_start", "material_id": material_id}
        if data.startswith("advertiser:batch:toggle:"):
            channel_id = data.removeprefix("advertiser:batch:toggle:")
            self._toggle_batch_channel(chat_id, user, channel_id, message)
            return {"handled": True, "type": "callback_batch_toggle", "channel_id": channel_id}
        if data == "advertiser:batch:budget":
            self._begin_batch_budget_input(chat_id, user, message)
            return {"handled": True, "type": "callback_batch_budget_prompt"}
        if data == "advertiser:batch:submit":
            return self._submit_batch_orders(chat_id, user, message)
        if data == "advertiser:batch:cancel":
            self._clear_batch_orders_state(chat_id)
            self._send_advertiser_library(chat_id, user, message)
            return {"handled": True, "type": "callback_batch_cancelled"}
        if data == "advertiser:orders":
            self._send_advertiser_orders(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_orders"}
        if data.startswith("advertiser:order:"):
            tail = data.removeprefix("advertiser:order:")
            order_id, _, action = tail.partition(":")
            if action == "stop":
                self._send_advertiser_order_stop_confirm(chat_id, user, order_id, message)
                return {"handled": True, "type": "callback_advertiser_order_stop_confirm", "order_id": order_id}
            if action == "stop_yes":
                return self._advertiser_stop_order(chat_id, user, order_id, message)
            if action == "dispute":
                self._begin_advertiser_dispute(chat_id, user, order_id, message)
                return {"handled": True, "type": "callback_advertiser_dispute_start", "order_id": order_id}
            self._send_advertiser_order_detail(chat_id, user, order_id, message)
            return {"handled": True, "type": "callback_advertiser_order_detail", "order_id": order_id}
        if data == "advertiser:order_help":
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text=(
                    "🧾 创建订单\n\n"
                    "从频道里的「频道招商」进入。\n\n"
                    "1 选广告位\n"
                    "2 发文案和链接\n"
                    "3 设置预算\n\n"
                    "✅ 发布成功才扣费"
                ),
                inline_keyboard=[[{"text": "📣 我的广告", "callback_data": "role:advertiser"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}]],
            )
            return {"handled": True, "type": "callback_advertiser_order_help"}
        if data == "publisher:onboard":
            self._start_publisher_onboarding(chat_id, user, message)
            return {"handled": True, "type": "callback_publisher_onboard_started"}
        if data == "publisher:pricing":
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text=(
                    "💵 定价\n\n"
                    "基准价由插播评估：类目、触达、点击、复投、争议、履约信用。\n\n"
                    "📉 低档：更容易接单\n"
                    "⚖️ 中档：默认推荐\n"
                    "📈 高档：单价高，成交更少"
                ),
                inline_keyboard=[[{"text": "📺 频道管理", "callback_data": "publisher:channels"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}]],
            )
            return {"handled": True, "type": "callback_publisher_pricing"}
        if data.startswith("pub:formats:"):
            channel_identifier = data.removeprefix("pub:formats:")
            self._send_publisher_formats(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_formats", "channel": channel_identifier}
        if data.startswith("pub:channel:"):
            channel_identifier = data.removeprefix("pub:channel:")
            self._send_publisher_channel_dashboard(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_channel", "channel": channel_identifier}
        if data.startswith("pub:refresh:"):
            channel_identifier = data.removeprefix("pub:refresh:")
            self._refresh_publisher_channel(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_channel_refreshed", "channel": channel_identifier}
        if data.startswith("pub:toggle:"):
            rest = data.removeprefix("pub:toggle:")
            channel_identifier, slot_type = rest.rsplit(":", 1)
            self._toggle_publisher_format(chat_id, user, channel_identifier, slot_type, message)
            return {"handled": True, "type": "callback_publisher_format_toggled", "channel": channel_identifier, "slot_type": slot_type}
        if data.startswith("pub:approval:"):
            channel_identifier = data.removeprefix("pub:approval:")
            self._send_publisher_approval_settings(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_approval", "channel": channel_identifier}
        if data.startswith("pub:band:set:"):
            rest = data.removeprefix("pub:band:set:")
            channel_identifier, format_type, band = rest.rsplit(":", 2)
            self._set_publisher_band(chat_id, user, channel_identifier, format_type, band, message)
            return {"handled": True, "type": "callback_publisher_band_set", "channel": channel_identifier, "format_type": format_type, "band": band}
        if data.startswith("pub:band:"):
            channel_identifier = data.removeprefix("pub:band:")
            self._send_publisher_band_picker(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_band_picker", "channel": channel_identifier}
        if data.startswith("pub:limit:set:"):
            rest = data.removeprefix("pub:limit:set:")
            channel_identifier, limit_str = rest.rsplit(":", 1)
            self._set_publisher_daily_limit(chat_id, user, channel_identifier, int(limit_str), message)
            return {"handled": True, "type": "callback_publisher_limit_set", "channel": channel_identifier, "limit": int(limit_str)}
        if data.startswith("pub:limit:"):
            channel_identifier = data.removeprefix("pub:limit:")
            self._send_publisher_limit_panel(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_limit_panel", "channel": channel_identifier}
        if data.startswith("pub:self:pick:"):
            rest = data.removeprefix("pub:self:pick:")
            channel_identifier, material_id = rest.rsplit(":", 1)
            return self._publish_self_promo(chat_id, user, channel_identifier, material_id, message)
        if data.startswith("pub:self:"):
            channel_identifier = data.removeprefix("pub:self:")
            self._send_publisher_self_promo_panel(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_self_promo", "channel": channel_identifier}
        if data.startswith("pub:stats:"):
            channel_identifier = data.removeprefix("pub:stats:")
            self._send_publisher_channel_stats(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_channel_stats", "channel": channel_identifier}
        if data.startswith("pub:earnings:"):
            channel_identifier = data.removeprefix("pub:earnings:")
            self._send_publisher_channel_earnings(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_channel_earnings", "channel": channel_identifier}
        if data.startswith("channel:quote:"):
            channel_id = data.removeprefix("channel:quote:")
            self._send_channel_quote(chat_id, channel_id, message)
            return {"handled": True, "type": "callback_channel_quote", "channel_id": channel_id}
        if data.startswith("channel:order:"):
            channel_id = data.removeprefix("channel:order:")
            self._start_order_flow(chat_id, user, channel_id, message)
            return {"handled": True, "type": "callback_order_flow_started", "channel_id": channel_id}
        if data.startswith("place:"):
            return self._handle_placement_callback(data, chat_id, user, message)
        if data.startswith("order:slot:"):
            rest = data.removeprefix("order:slot:")
            channel_id, slot_type = rest.rsplit(":", 1)
            self._set_order_slot(chat_id, user, channel_id, slot_type, message)
            return {"handled": True, "type": "callback_order_slot_selected", "channel_id": channel_id, "slot_type": slot_type}
        if data.startswith("order:pick:"):
            index = int(data.removeprefix("order:pick:"))
            self._select_order_creative(chat_id, user, index, message)
            return {"handled": True, "type": "callback_order_creative_selected", "index": index}
        if data == "order:newcreative":
            self._begin_new_order_creative(chat_id, user, message)
            return {"handled": True, "type": "callback_order_new_creative"}
        if data == "order:cancel":
            self._clear_conversation(chat_id)
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text="✅ 已取消",
                inline_keyboard=[[{"text": "🏠 主菜单", "callback_data": "menu:home"}]],
            )
            return {"handled": True, "type": "callback_order_cancelled"}
        if data == "flow:cancel":
            self._clear_conversation(chat_id)
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text="✅ 已取消",
                inline_keyboard=[[{"text": "🏠 主菜单", "callback_data": "menu:home"}]],
            )
            return {"handled": True, "type": "callback_flow_cancelled"}

        self._reply_or_edit(
            chat_id=chat_id,
            source_message=message,
            text="⚠️ 这个按钮暂时不可用",
            inline_keyboard=[[{"text": "🏠 主菜单", "callback_data": "menu:home"}]],
        )
        return {"handled": True, "type": "callback_unknown", "data": data}

    def _handle_my_chat_member(self, event: dict[str, Any]) -> dict[str, Any]:
        chat = event.get("chat") or {}
        if chat.get("type") != "channel":
            return {"handled": False, "reason": "unsupported_chat_type"}
        new_member = event.get("new_chat_member") or {}
        status = new_member.get("status", "unknown")
        if status in {"left", "kicked"}:
            with self.db.transaction() as conn:
                channel = self.channels.get_by_chat_id(conn, chat.get("id"))
                if channel:
                    conn.execute("UPDATE channels SET status = 'inactive', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (channel["id"],))
            return {"handled": True, "type": "bot_removed_from_channel", "status": status}
        if status not in {"administrator", "creator", "member"}:
            return {"handled": False, "reason": "unsupported_member_status", "status": status}

        actor = event.get("from") or {}
        actor_id = actor.get("id")
        if not actor_id:
            return {"handled": False, "reason": "missing_actor"}
        display_name = self._display_name(actor)
        channel = self.channels.bind_channel(
            telegram_chat_id=chat["id"],
            title=chat.get("title") or chat.get("username") or "未命名频道",
            username=self._normalize_username(chat.get("username")),
            owner_telegram_user_id=actor_id,
            owner_display_name=display_name,
        )
        admins = self._sync_channel_admins(channel["id"], chat["id"])
        notified = self._notify_channel_admins(channel, admins, actor_id)
        return {"handled": True, "type": "bot_channel_asset_added", "channel_id": channel["id"], "admins": len(admins), "notified": notified}

    def _handle_chat_member(self, event: dict[str, Any]) -> dict[str, Any]:
        chat = event.get("chat") or {}
        if chat.get("type") != "channel":
            return {"handled": False, "reason": "unsupported_chat_type"}
        with self.db.transaction() as conn:
            channel = self.channels.get_by_chat_id(conn, chat.get("id"))
            if not channel:
                return {"handled": False, "reason": "channel_not_bound"}
            new_member = event.get("new_chat_member") or {}
            user = new_member.get("user") or {}
            if not user.get("id"):
                return {"handled": False, "reason": "missing_member_user"}
            self.channels.record_channel_admin(
                conn,
                channel_id=channel["id"],
                telegram_user_id=user["id"],
                status=new_member.get("status", "unknown"),
                display_name=self._display_name(user),
                is_bot=bool(user.get("is_bot")),
                can_post_messages=bool(new_member.get("can_post_messages")),
                can_edit_messages=bool(new_member.get("can_edit_messages")),
                can_pin_messages=bool(new_member.get("can_pin_messages")),
            )
        return {"handled": True, "type": "channel_admin_synced", "channel_id": channel["id"]}

    def _handle_channel_post(self, post: dict[str, Any]) -> dict[str, Any]:
        chat = post.get("chat") or {}
        chat_id = chat.get("id")
        message_id = post.get("message_id")
        if not chat_id or not message_id:
            return {"handled": False, "reason": "missing_channel_post_ids"}
        with self.db.transaction() as conn:
            channel = self.channels.get_by_chat_id(conn, chat_id)
            if not channel:
                return {"handled": False, "reason": "channel_not_bound"}
            probe = self.light_probes.get_active_for_channel(conn, channel["id"])
            keyboard = self._merge_channel_buttons(post, self.channels.start_url(channel), probe)
            try:
                self.gateway.edit_message_reply_markup(
                    chat_id=str(chat_id),
                    message_id=message_id,
                    inline_keyboard=keyboard,
                )
            except TelegramError as exc:
                conn.execute(
                    """
                    INSERT INTO audit_logs (id, action, entity_type, entity_id, payload_json)
                    VALUES (?, 'append_button_failed', 'channel', ?, ?)
                    """,
                    (new_id("aud"), channel["id"], json.dumps({"error": str(exc), "message_id": message_id}, ensure_ascii=False)),
                )
                return {"handled": False, "reason": "append_button_failed", "error": str(exc)}
            post_text = post.get("text") or post.get("caption")
            if post_text:
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
                                "chat_id": str(chat_id),
                                "message_id": str(message_id),
                                "text": post_text,
                                "inline_keyboard": keyboard,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            return {"handled": True, "type": "channel_post_button_appended", "channel_id": channel["id"]}

    def _handle_pre_checkout_query(self, query: dict[str, Any]) -> dict[str, Any]:
        validation = self.stars_payments.validate_pre_checkout(query)
        self.gateway.answer_pre_checkout_query(
            pre_checkout_query_id=query["id"],
            ok=validation["ok"],
            error_message=validation.get("error_message"),
        )
        if not validation["ok"]:
            return {"handled": True, "type": "pre_checkout_rejected", "reason": validation["error_message"]}
        return {"handled": True, "type": "pre_checkout_approved", "payload": query.get("invoice_payload")}

    def _send_main_menu(
        self,
        chat_id: str | int,
        source_message: dict[str, Any] | None = None,
        user: dict[str, Any] | None = None,
    ) -> None:
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "📌 插播工作台\n\n"
                "接广告，投频道。"
            ),
            inline_keyboard=[
                [{"text": "➕ 添加频道", "url": self._add_channel_url()}],
                [{"text": "📺 频道管理", "callback_data": "publisher:channels"}, {"text": "📣 我的广告", "callback_data": "role:advertiser"}],
                [{"text": "💸 我的收益", "callback_data": "publisher:earnings"}, {"text": "💰 广告钱包", "callback_data": "advertiser:balance"}],
                [{"text": "💵 定价规则", "callback_data": "publisher:pricing"}, {"text": "🌐 时区", "callback_data": "timezone:change"}],
            ],
        )

    def _send_advertiser_menu(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", user.get("first_name") or user.get("username"))
            balance = account["available_balance_cents"] / 100
            reserved = account["reserved_balance_cents"] / 100
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "📣 我的广告\n\n"
                f"💰 可用：USD {balance:.2f}\n"
                f"🔒 冻结：USD {reserved:.2f}"
            ),
            inline_keyboard=[
                [{"text": "🔍 找频道", "callback_data": "advertiser:discover"}, {"text": "⭐ 我的收藏", "callback_data": "advertiser:saved"}],
                [{"text": "🗂 广告库", "callback_data": "advertiser:library"}, {"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                [{"text": "💰 广告钱包", "callback_data": "advertiser:balance"}, {"text": "📦 我的套餐", "callback_data": "advertiser:plan"}],
                [{"text": "💵 定价规则", "callback_data": "publisher:pricing"}, {"text": "🧾 创建广告", "callback_data": "advertiser:order_help"}],
                [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ],
        )

    DISCOVER_LIST_NUMBERS = ("①", "②", "③", "④", "⑤")
    DISCOVER_PAGE_LIMIT = 5
    DISCOVER_RISK_EMOJI = {
        "normal": "🟢",
        "watch": "🟡",
        "high": "🟠",
        "blocked": "🔴",
    }

    def _send_advertiser_discover(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        try:
            channels = self.advertisers.discover_channels(
                advertiser_telegram_user_id=user_id,
                slot_type="standard_card",
                limit=self.DISCOVER_PAGE_LIMIT,
            )
        except Exception as exc:
            logger.warning("discover_channels_failed user=%s error=%s", user_id, exc)
            channels = []

        lines = ["🔍 找频道", ""]
        if not channels:
            lines.extend([
                "暂无符合条件的频道。",
                "",
                "想绕开列表?也可以从频道帖底部的「📣 频道招商」按钮进入。",
            ])
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="\n".join(lines),
                inline_keyboard=[
                    [{"text": "📣 我的广告", "callback_data": "role:advertiser"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return

        try:
            saved_ids = {row["channel_id"] for row in self.advertisers.list_saved_channels(user_id)}
        except Exception:
            saved_ids = set()

        for index, channel in enumerate(channels):
            number = self.DISCOVER_LIST_NUMBERS[index] if index < len(self.DISCOVER_LIST_NUMBERS) else f"{index + 1}."
            risk_emoji = self.DISCOVER_RISK_EMOJI.get(channel.get("risk_level"), "⚪️")
            category_label = self.CATEGORY_VALUE.get(channel.get("category") or "general", ("通用", ""))[0]
            subscribers = int(channel.get("subscribers") or 0)
            views = int(channel.get("median_24h_views") or 0)
            price = cents_to_money(int(channel.get("list_price_cents") or 0))
            saved_mark = " ⭐" if channel["channel_id"] in saved_ids else ""
            lines.append(f"{number} {channel['title']}{saved_mark}")
            lines.append(
                f"   🏷 {category_label} · {risk_emoji} 风控 · ⭐ {channel.get('score') or 0}"
            )
            lines.append(
                f"   👥 {subscribers:,} 订阅 · 📈 {views:,} 浏览/24h · 💵 标准插播 USD {price}"
            )
        lines.append("")
        lines.append("▶ 进入投放配置｜⭐ 收藏频道。")

        action_buttons = [
            {
                "text": f"▶ {self.DISCOVER_LIST_NUMBERS[i] if i < len(self.DISCOVER_LIST_NUMBERS) else str(i + 1)}",
                "callback_data": f"channel:order:{channel['channel_id']}",
            }
            for i, channel in enumerate(channels)
        ]
        save_buttons = [
            {
                "text": ("🌟 " if channel["channel_id"] in saved_ids else "⭐ ")
                + (self.DISCOVER_LIST_NUMBERS[i] if i < len(self.DISCOVER_LIST_NUMBERS) else str(i + 1)),
                "callback_data": f"advertiser:save:disc:{channel['channel_id']}",
            }
            for i, channel in enumerate(channels)
        ]
        keyboard = [action_buttons, save_buttons]
        keyboard.append([
            {"text": "⭐ 我的收藏", "callback_data": "advertiser:saved"},
        ])
        keyboard.append([
            {"text": "📣 我的广告", "callback_data": "role:advertiser"},
            {"text": "🏠 主菜单", "callback_data": "menu:home"},
        ])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _send_advertiser_saved(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        try:
            saved = self.advertisers.list_saved_channels(user_id)
        except Exception as exc:
            logger.warning("list_saved_channels_failed user=%s error=%s", user_id, exc)
            saved = []
        lines = ["⭐ 我的收藏", ""]
        if not saved:
            lines.extend([
                "还没有收藏频道。",
                "在「🔍 找频道」里看到合适的就按 ⭐ 收藏。",
            ])
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="\n".join(lines),
                inline_keyboard=[
                    [{"text": "🔍 找频道", "callback_data": "advertiser:discover"}],
                    [{"text": "📣 我的广告", "callback_data": "role:advertiser"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return

        play_buttons: list[dict[str, str]] = []
        unsave_buttons: list[dict[str, str]] = []
        for index, row in enumerate(saved[: len(self.DISCOVER_LIST_NUMBERS)]):
            number = self.DISCOVER_LIST_NUMBERS[index]
            label = row["title"] or row.get("username") or row["channel_id"]
            note = (row.get("note") or "").strip()
            line = f"{number} {label}"
            if note:
                line += f" · 备注：{note}"
            lines.append(line)
            play_buttons.append({
                "text": f"▶ {number}",
                "callback_data": f"channel:order:{row['channel_id']}",
            })
            unsave_buttons.append({
                "text": f"❌ {number}",
                "callback_data": f"advertiser:save:saved:{row['channel_id']}",
            })

        keyboard = [play_buttons, unsave_buttons]
        keyboard.append([
            {"text": "🔔 新频道提醒", "callback_data": "advertiser:alerts"},
        ])
        keyboard.append([
            {"text": "🔍 找频道", "callback_data": "advertiser:discover"},
            {"text": "🏠 主菜单", "callback_data": "menu:home"},
        ])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _send_advertiser_alerts(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        try:
            events = self.advertisers.list_alert_events(user_id, status=None)
        except InvalidState as exc:
            # Free tier — no alerts feature
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=(
                    "🔔 新频道提醒\n\n"
                    f"ℹ️ {exc}\n"
                    "升级到 Pro 套餐后可设置提醒规则，新评估的频道达标会自动通知。\n"
                    "目前可在 CLI 使用 `chabo create-alert-rule` 创建规则。"
                ),
                inline_keyboard=[
                    [{"text": "⭐ 我的收藏", "callback_data": "advertiser:saved"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return
        except Exception as exc:
            logger.warning("list_alert_events_failed user=%s error=%s", user_id, exc)
            events = []

        lines = ["🔔 新频道提醒", ""]
        if not events:
            lines.extend([
                "暂无新提醒。",
                "",
                "提醒规则目前在 CLI 创建：",
                "`chabo create-alert-rule --advertiser-telegram-user-id <你的 ID> --category <类目> --min-score 70`",
            ])
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="\n".join(lines),
                inline_keyboard=[
                    [{"text": "⭐ 我的收藏", "callback_data": "advertiser:saved"}],
                    [{"text": "🔍 找频道", "callback_data": "advertiser:discover"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return

        play_buttons: list[dict[str, str]] = []
        for index, event in enumerate(events[: len(self.DISCOVER_LIST_NUMBERS)]):
            number = self.DISCOVER_LIST_NUMBERS[index]
            risk_emoji = self.DISCOVER_RISK_EMOJI.get(event.get("risk_level"), "⚪️")
            category_label = self.CATEGORY_VALUE.get(event.get("category") or "general", ("通用", ""))[0]
            status_mark = "🆕" if event.get("status") == "new" else "·"
            lines.append(
                f"{number} {status_mark} {event['title']}"
                f"｜🏷 {category_label}｜{risk_emoji}｜⭐ {event.get('score') or 0}"
            )
            play_buttons.append({
                "text": f"▶ {number}",
                "callback_data": f"channel:order:{event['channel_id']}",
            })

        keyboard = [play_buttons]
        keyboard.append([
            {"text": "⭐ 我的收藏", "callback_data": "advertiser:saved"},
            {"text": "🏠 主菜单", "callback_data": "menu:home"},
        ])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    PLAN_LABELS = {
        "free": "Free（免费）",
        "pro": "Pro（专业）",
        "enterprise": "Enterprise（企业）",
    }

    def _send_advertiser_plan_panel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        try:
            status = self.advertiser_subscriptions.status(user_id)
        except Exception as exc:
            logger.warning("plan_status_failed user=%s error=%s", user_id, exc)
            status = {
                "subscription": None,
                "entitlements": {"plan": "free", **AdvertiserSubscriptionService.FREE_LIMITS},
            }
        entitlements = status.get("entitlements") or {"plan": "free"}
        current_plan = entitlements.get("plan", "free")
        active = status.get("subscription")

        lines = ["📦 我的套餐", ""]
        lines.append(f"当前：{self.PLAN_LABELS.get(current_plan, current_plan)}")
        if active and active.get("expires_at"):
            tz_name = self._account_timezone(user_id)
            lines.append(f"到期：{self._format_local_time(active['expires_at'], tz_name)}")
        lines.append("")
        lines.append("权益对比：")
        for plan_key, plan in (
            ("free", {"monthly_price_cents": 0, **AdvertiserSubscriptionService.FREE_LIMITS}),
            ("pro", AdvertiserSubscriptionService.PLANS["pro"]),
            ("enterprise", AdvertiserSubscriptionService.PLANS["enterprise"]),
        ):
            label = self.PLAN_LABELS.get(plan_key, plan_key)
            price_cents = int(plan.get("monthly_price_cents") or 0)
            price = "免费" if price_cents == 0 else f"USD {cents_to_money(price_cents)} / 月"
            badge = "（当前）" if plan_key == current_plan else ""
            lines.append(f"• {label}{badge}｜{price}")
            lines.append(
                f"   频道发现 {plan.get('discover_limit')}｜批量 {'✅' if plan.get('batch_orders') else '❌'}"
                f"｜提醒 {'✅' if plan.get('alerts') else '❌'}｜完整报表 {'✅' if plan.get('full_report') else '❌'}"
            )

        keyboard: list[list[dict[str, str]]] = []
        upgrade_buttons: list[dict[str, str]] = []
        for plan_key in ("pro", "enterprise"):
            if plan_key == current_plan:
                continue
            label = self.PLAN_LABELS.get(plan_key, plan_key)
            price_cents = int(AdvertiserSubscriptionService.PLANS[plan_key]["monthly_price_cents"])
            upgrade_buttons.append({
                "text": f"🚀 {label} · USD {cents_to_money(price_cents)}",
                "callback_data": f"advertiser:plan:buy:{plan_key}",
            })
        if upgrade_buttons:
            keyboard.append(upgrade_buttons)
        keyboard.append([
            {"text": "💰 广告钱包", "callback_data": "advertiser:balance"},
            {"text": "🏠 主菜单", "callback_data": "menu:home"},
        ])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _trigger_advertiser_plan_purchase(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        plan: str,
        source_message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_id = user.get("id") or chat_id
        try:
            invoice_response = self.stars_payments.create_advertiser_subscription_invoice(
                advertiser_telegram_user_id=user_id,
                plan=plan,
                months=1,
            )
        except (NotFound, InvalidState, ChaboError) as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 无法发起套餐购买：{exc}",
                inline_keyboard=[[{"text": "📦 返回套餐", "callback_data": "advertiser:plan"}]],
            )
            return {"handled": True, "type": "callback_advertiser_plan_invoice_failed", "error": str(exc)}
        invoice = invoice_response["invoice"]
        try:
            self.gateway.send_invoice(
                chat_id=user_id,
                title=invoice["title"],
                description=invoice["description"],
                payload=invoice["payload"],
                currency=invoice["currency"],
                prices=invoice["prices"],
            )
        except TelegramError as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 发票发送失败：{exc}",
                inline_keyboard=[[{"text": "📦 返回套餐", "callback_data": "advertiser:plan"}]],
            )
            return {"handled": True, "type": "callback_advertiser_plan_invoice_failed", "error": str(exc)}
        plan_label = self.PLAN_LABELS.get(plan, plan)
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                f"💳 已发送 {plan_label} 套餐发票\n\n"
                "在 Telegram 内打开发票完成 Stars 支付，到账后回到「📦 我的套餐」查看权益。"
            ),
            inline_keyboard=[[{"text": "📦 返回套餐", "callback_data": "advertiser:plan"}]],
        )
        return {
            "handled": True,
            "type": "callback_advertiser_plan_invoice_sent",
            "plan": plan,
            "stars_amount": invoice_response["intent"]["stars_amount"],
        }

    def _account_timezone(self, telegram_user_id: str | int) -> str:
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT timezone FROM accounts WHERE telegram_user_id = ?",
                    (str(telegram_user_id),),
                ).fetchone()
        except Exception:
            return DEFAULT_USER_TIMEZONE
        if row and row["timezone"]:
            return row["timezone"]
        return DEFAULT_USER_TIMEZONE

    def _toggle_saved_channel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_id: str,
        origin: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        try:
            already = self.advertisers.is_saved_channel(
                advertiser_telegram_user_id=user_id,
                channel_id=channel_id,
            )
            if already:
                self.advertisers.remove_saved_channel(
                    advertiser_telegram_user_id=user_id,
                    channel_id=channel_id,
                )
            else:
                self.advertisers.save_channel(
                    advertiser_telegram_user_id=user_id,
                    channel_id=channel_id,
                )
        except (NotFound, InvalidState) as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 操作失败：{exc}",
                inline_keyboard=[
                    [{"text": "⭐ 我的收藏", "callback_data": "advertiser:saved"}],
                    [{"text": "🔍 找频道", "callback_data": "advertiser:discover"}],
                ],
            )
            return
        if origin == "saved":
            self._send_advertiser_saved(chat_id, user, source_message)
        else:
            self._send_advertiser_discover(chat_id, user, source_message)

    def _send_channel_sales_landing(
        self,
        chat_id: str | int,
        channel: dict[str, Any],
        rates: list[Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        del rates
        payload = self._default_placement_payload(channel["id"])
        self._send_placement_configurator(chat_id, {}, source_message, payload=payload)

    def _default_placement_payload(self, channel_id: str) -> dict[str, Any]:
        return {
            "channel_id": channel_id,
            "slot_type": "",
            "pin": False,
            "period": "once",
            "scheduled_label": "立即发布",
            "creative_text": "",
            "target_url": "",
            "button_text": "打开链接",
            "creative_ids": [],
        }

    def _send_placement_configurator(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        panel: str = "home",
    ) -> None:
        user_id = user.get("id") or chat_id
        display_name = self._display_name(user) or None
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", display_name)
            if payload is None:
                state = conn.execute("SELECT * FROM bot_conversation_states WHERE chat_id = ?", (str(chat_id),)).fetchone()
                if not state or state["flow"] != "placement_config":
                    self._reply_or_edit(
                        chat_id=chat_id,
                        source_message=source_message,
                        text="⚠️ 当前投放配置已失效，请从频道入口重新开始。",
                        inline_keyboard=[[{"text": "🏠 工作台", "callback_data": "menu:home"}]],
                    )
                    return
                payload = json.loads(state["payload_json"] or "{}")
            channel = self._sync_channel_profile_conn(conn, self.channels.get_channel(conn, payload["channel_id"]))
            creatives: list[dict[str, Any]] = []
            if panel == "creative":
                slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
                format_type = slot_type if slot_type in PLACEMENT_SLOT_TYPES else None
                creatives = self.materials.list_materials_in_conn(
                    conn,
                    advertiser_account_id=account["id"],
                    format_type=format_type,
                    limit=5,
                )
                payload["creative_ids"] = [row["id"] for row in creatives]
            self._set_conversation_conn(conn, chat_id, account["id"], "placement_config", panel, payload)

        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=self._placement_text(channel, payload, panel),
            inline_keyboard=self._placement_keyboard(panel, payload, creatives),
        )

    def _handle_placement_callback(self, data: str, chat_id: str | int, user: dict[str, Any], message: dict[str, Any] | None = None) -> dict[str, Any]:
        if data == "place:home":
            self._send_placement_configurator(chat_id, user, message, panel="home")
            return {"handled": True, "type": "callback_placement_home"}
        if data in {"place:display", "place:schedule", "place:creative", "place:confirm"}:
            panel = data.removeprefix("place:")
            self._send_placement_configurator(chat_id, user, message, panel=panel)
            return {"handled": True, "type": f"callback_placement_{panel}"}

        state = self._get_conversation(chat_id)
        if not state or state["flow"] != "placement_config":
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text="⚠️ 当前投放配置已失效，请从频道入口重新开始。",
                inline_keyboard=[[{"text": "🏠 工作台", "callback_data": "menu:home"}]],
            )
            return {"handled": True, "type": "callback_placement_expired"}
        payload = json.loads(state["payload_json"] or "{}")

        if data.startswith("place:slot:"):
            slot_type = self.channels.normalize_slot_type(data.removeprefix("place:slot:"))
            previous_slot = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            payload["slot_type"] = slot_type
            if slot_type not in PINNABLE_PLACEMENT_SLOTS:
                payload["pin"] = False
            if slot_type != previous_slot:
                self._swap_placement_creative_draft(payload, previous_slot, slot_type)
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel="display")
            return {"handled": True, "type": "callback_placement_slot", "slot_type": slot_type}

        if data == "place:pin":
            slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            if slot_type in PINNABLE_PLACEMENT_SLOTS:
                payload["pin"] = not bool(payload.get("pin"))
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel="display")
            return {"handled": True, "type": "callback_placement_pin", "pin": bool(payload.get("pin"))}

        if data.startswith("place:period:"):
            period = data.removeprefix("place:period:")
            if period in PLACEMENT_PERIODS:
                payload["period"] = period
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel="schedule")
            return {"handled": True, "type": "callback_placement_period", "period": payload.get("period")}

        if data == "place:time":
            self._set_conversation(chat_id, state["account_id"], "placement_config", "time_input", payload)
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text=(
                    "🕒 设置发布时间\n\n"
                    "请发送发布时间。\n"
                    "例如：今天 16:00、明天 14:30、2026-04-30 20:00\n\n"
                    "当前时区按你的账户设置计算。"
                ),
                inline_keyboard=self._cancel_keyboard("取消设置", "place:home"),
            )
            return {"handled": True, "type": "callback_placement_time_input"}

        if data.startswith("place:pick:"):
            index = int(data.removeprefix("place:pick:"))
            creative_ids = payload.get("creative_ids") or []
            if index < 0 or index >= len(creative_ids):
                self._reply_or_edit(chat_id=chat_id, source_message=message, text="⚠️ 没有这个广告素材。", inline_keyboard=[[{"text": "📁 广告素材", "callback_data": "place:creative"}]])
                return {"handled": True, "type": "callback_placement_creative_missing"}
            with self.db.transaction() as conn:
                creative = conn.execute("SELECT * FROM creatives WHERE id = ?", (creative_ids[index],)).fetchone()
            if not creative or creative["archived_at"]:
                self._reply_or_edit(chat_id=chat_id, source_message=message, text="⚠️ 广告素材已不可用。", inline_keyboard=[[{"text": "📁 广告素材", "callback_data": "place:creative"}]])
                return {"handled": True, "type": "callback_placement_creative_missing"}
            payload.update(
                {
                    "material_id": creative["id"],
                    "creative_text": creative["text"],
                    "target_url": creative["target_url"],
                    "button_text": creative["button_text"],
                    "light_short_text": creative["light_short_text"],
                }
            )
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel="home")
            return {"handled": True, "type": "callback_placement_creative_selected", "material_id": creative["id"]}

        if data.startswith("place:archive:"):
            index = int(data.removeprefix("place:archive:"))
            creative_ids = payload.get("creative_ids") or []
            if index < 0 or index >= len(creative_ids):
                self._send_placement_configurator(chat_id, user, message, payload=payload, panel="creative")
                return {"handled": True, "type": "callback_placement_archive_missing"}
            target_id = creative_ids[index]
            try:
                with self.db.transaction() as conn:
                    self.materials.archive_material_in_conn(
                        conn,
                        material_id=target_id,
                        advertiser_account_id=state["account_id"],
                    )
            except NotFound:
                self._send_placement_configurator(chat_id, user, message, payload=payload, panel="creative")
                return {"handled": True, "type": "callback_placement_archive_missing"}
            if payload.get("material_id") == target_id:
                for key in ("material_id", "creative_text", "target_url", "button_text", "light_short_text"):
                    payload.pop(key, None)
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel="creative")
            return {"handled": True, "type": "callback_placement_creative_archived", "material_id": target_id}

        if data.startswith("place:new:"):
            requested_slot = data.removeprefix("place:new:")
            if requested_slot != "auto":
                payload["slot_type"] = self.channels.normalize_slot_type(requested_slot)
                if payload["slot_type"] not in PINNABLE_PLACEMENT_SLOTS:
                    payload["pin"] = False
            self._begin_placement_creative(chat_id, user, message, payload, state["account_id"])
            return {"handled": True, "type": "callback_placement_new_creative"}

        if data == "place:submit":
            return self._submit_placement_order(chat_id, user, message, payload)

        if data == "place:draft":
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel="home")
            return {"handled": True, "type": "callback_placement_draft_saved"}

        self._send_placement_configurator(chat_id, user, message, payload=payload, panel="home")
        return {"handled": True, "type": "callback_placement_unknown"}

    def _handle_placement_message(self, message: dict[str, Any], state: dict[str, Any], text: str) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        if not chat_id:
            return {"handled": False, "reason": "missing_chat"}
        if not text:
            self.gateway.send_private_message(chat_id=chat_id, text="请发送文字内容，或点击取消。", inline_keyboard=self._cancel_keyboard("取消设置", "place:home"))
            return {"handled": True, "type": "placement_text_required"}

        payload = json.loads(state["payload_json"] or "{}")
        clean_text = text.strip()
        step = state["step"]

        if step == "time_input":
            payload["scheduled_label"] = clean_text
            self._send_placement_configurator(chat_id, user, None, payload=payload, panel="home")
            return {"handled": True, "type": "placement_time_saved"}

        if step == "light_short_text":
            if len(clean_text) < 2 or len(clean_text) > 15:
                self.gateway.send_private_message(chat_id=chat_id, text="文字插播短入口需要 2-15 个字。", inline_keyboard=self._cancel_keyboard("取消创建", "place:home"))
                return {"handled": True, "type": "placement_invalid_light_short_text"}
            payload["light_short_text"] = clean_text
            payload["button_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "placement_config", "light_detail_text", payload)
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="✅ 短入口已保存\n\n请发送完整广告详情。用户点击文字插播后，会在 Bot 里看到这段内容。",
                inline_keyboard=self._cancel_keyboard("取消创建", "place:home"),
            )
            return {"handled": True, "type": "placement_light_short_saved"}

        if step == "light_detail_text":
            if len(clean_text) < 4 or len(clean_text) > 1000:
                self.gateway.send_private_message(chat_id=chat_id, text="广告详情需要 4-1000 个字。", inline_keyboard=self._cancel_keyboard("取消创建", "place:home"))
                return {"handled": True, "type": "placement_invalid_light_detail"}
            payload["creative_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "placement_config", "target_url", payload)
            self.gateway.send_private_message(chat_id=chat_id, text="请发送广告目标链接，必须以 http:// 或 https:// 开头。", inline_keyboard=self._cancel_keyboard("取消创建", "place:home"))
            return {"handled": True, "type": "placement_light_detail_saved"}

        if step == "creative_text":
            if len(clean_text) < 4 or len(clean_text) > 800:
                self.gateway.send_private_message(chat_id=chat_id, text="广告文案需要 4-800 个字。", inline_keyboard=self._cancel_keyboard("取消创建", "place:home"))
                return {"handled": True, "type": "placement_invalid_creative"}
            payload["creative_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "placement_config", "target_url", payload)
            self.gateway.send_private_message(chat_id=chat_id, text="请发送广告目标链接，必须以 http:// 或 https:// 开头。", inline_keyboard=self._cancel_keyboard("取消创建", "place:home"))
            return {"handled": True, "type": "placement_creative_saved"}

        if step == "target_url":
            if not (clean_text.startswith("https://") or clean_text.startswith("http://")):
                self.gateway.send_private_message(chat_id=chat_id, text="链接格式不对。请发送以 http:// 或 https:// 开头的目标链接。", inline_keyboard=self._cancel_keyboard("取消创建", "place:home"))
                return {"handled": True, "type": "placement_invalid_url"}
            payload["target_url"] = clean_text
            user_id = user.get("id") or chat_id
            slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "standard_card")
            format_type = slot_type if slot_type in PLACEMENT_SLOT_TYPES else "standard_card"
            try:
                material = self.materials.create_material(
                    advertiser_telegram_user_id=user_id,
                    format_type=format_type,
                    text=payload["creative_text"],
                    target_url=clean_text,
                    button_text=payload.get("button_text") or "查看详情",
                    light_short_text=payload.get("light_short_text"),
                    display_name=self._display_name(user) or None,
                )
            except (InvalidState, NotFound) as exc:
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text=f"⚠️ 素材保存失败：{exc}",
                    inline_keyboard=self._cancel_keyboard("取消创建", "place:home"),
                )
                return {"handled": True, "type": "placement_create_material_failed", "error": str(exc)}
            payload["material_id"] = material["id"]
            payload["creative_text"] = material["text"]
            payload["target_url"] = material["target_url"]
            payload["button_text"] = material["button_text"]
            payload["light_short_text"] = material["light_short_text"]
            self._send_placement_configurator(chat_id, user, None, payload=payload, panel="home")
            return {"handled": True, "type": "placement_url_saved", "material_id": material["id"]}

        return {"handled": False, "reason": "unknown_placement_step"}

    def _begin_placement_creative(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None,
        payload: dict[str, Any],
        account_id: str | None,
    ) -> None:
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "standard_card")
        payload["slot_type"] = slot_type
        first_step = "light_short_text" if slot_type == "light_tail" else "creative_text"
        self._set_conversation(chat_id, account_id, "placement_config", first_step, payload)
        if slot_type == "light_tail":
            text = (
                "✍️ 创建文字插播\n\n"
                "显示在频道内容帖底部。\n"
                "短入口最多 15 个字，点击后打开完整广告详情。\n\n"
                "请发送短入口。"
            )
        else:
            text = (
                f"➕ 创建{self._slot_name(slot_type)}素材\n\n"
                "请发送广告文案。\n"
                "后续会补标题、图片/视频和按钮文案。"
            )
        self._reply_or_edit(chat_id=chat_id, source_message=source_message, text=text, inline_keyboard=self._cancel_keyboard("取消创建", "place:home"))

    def _submit_placement_order(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
        missing_panel = self._placement_missing_panel(payload)
        if missing_panel:
            self._send_placement_configurator(chat_id, user, source_message, payload=payload, panel=missing_panel)
            return {"handled": True, "type": "callback_placement_submit_incomplete", "missing": missing_panel}

        user_id = user.get("id") or chat_id
        quote = self._placement_quote(payload)
        scheduled_at = datetime.now(timezone.utc)
        period = PLACEMENT_PERIODS.get(payload.get("period") or "once", PLACEMENT_PERIODS["once"])
        deliveries = int(period["deliveries"])
        end_at = scheduled_at + timedelta(days=deliveries - 1) if deliveries > 1 else None
        order_slot_type = "pin24h" if payload.get("pin") else self.channels.normalize_slot_type(payload["slot_type"])
        try:
            with self.db.transaction() as conn:
                channel = self.channels.get_channel(conn, payload["channel_id"])
            order = self.orders.create_order(
                advertiser_telegram_user_id=user_id,
                channel_token=channel["ref_token"],
                slot_type=order_slot_type,
                material_id=payload["material_id"],
                button_text=payload.get("button_text") or "打开链接",
                budget_cents=quote["total_cents"],
                scheduled_at=scheduled_at,
                end_at=end_at,
                frequency_per_day=1,
                unit_price_override_cents=quote["unit_cents"],
                campaign_name="Bot 自助插播广告",
            )
            if self.settings.bot_auto_approve_orders:
                order = self.orders.approve_order(order["id"])
        except ChaboError as exc:
            short = str(exc)
            insufficient = isinstance(exc, InsufficientBalance) or "余额" in short
            primary_row: list[dict[str, str]] = []
            if insufficient:
                primary_row.append({"text": "💳 立即充值", "callback_data": "wallet:topup"})
            primary_row.append({"text": "🧩 降低配置", "callback_data": "place:display"})
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 暂时无法提交投放\n\n{exc}",
                inline_keyboard=[
                    primary_row,
                    [{"text": "💰 广告钱包", "callback_data": "advertiser:balance"}, {"text": "🏠 工作台", "callback_data": "menu:home"}],
                ],
            )
            return {"handled": True, "type": "callback_placement_submit_failed", "error": short}

        self._clear_conversation(chat_id)
        review_text = "✅ 已自动通过审核\n🚀 已进入排期" if self.settings.bot_auto_approve_orders else "⏳ 等待审核"
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "✅ 投放已提交\n\n"
                f"订单：{order['id']}\n"
                f"频道：{channel['title']}\n"
                f"展示：{self._placement_display_label(payload)}\n"
                f"发布：{self._placement_period_label(payload)}\n"
                f"冻结：USD {cents_to_money(quote['total_cents'])}\n\n"
                f"{review_text}"
            ),
            inline_keyboard=[
                [{"text": "📋 查看订单", "callback_data": "advertiser:orders"}],
                [{"text": "📣 继续投放这个频道", "callback_data": f"channel:order:{channel['id']}"}],
                [{"text": "🏠 工作台", "callback_data": "menu:home"}],
            ],
        )
        return {"handled": True, "type": "callback_placement_order_created", "order_id": order["id"]}

    def _placement_text(self, channel: dict[str, Any], payload: dict[str, Any], panel: str) -> str:
        lines = [
            f"🎯 给「{channel['title']}」投放广告",
        ]
        lines.extend(self._channel_quality_lines(channel["id"]))
        lines.extend([
            "",
            f"展示：{self._placement_display_label(payload)}",
            f"发布：{self._placement_period_label(payload)}",
            f"时间：{payload.get('scheduled_label') or '立即发布'}",
            f"素材：{self._placement_creative_label(payload)}",
            f"费用：{self._placement_cost_label(payload)}",
            "",
            f"下一步：{self._placement_next_step(payload)}",
        ])
        if panel == "display":
            lines.extend(["", "🧩 展示设置", "选择广告在频道里的呈现方式。"])
        elif panel == "schedule":
            lines.extend(["", "⏱ 发布设置", "默认立即发布，每 24 小时重复一次。"])
        elif panel == "creative":
            lines.extend(["", "📁 广告素材", "选择已有素材，或创建一条新素材。"])
        elif panel == "confirm":
            lines.extend(["", "✅ 费用确认"])
            lines.extend(self._placement_cost_breakdown(channel, payload))
            lines.append("确认后冻结预算，发布成功才扣费。")
        return "\n".join(lines)

    # 类目商业价值: 施工图 §8.4 — 高 / 中高 / 中 / 中低 / 低 / 高风险
    CATEGORY_VALUE = {
        "finance": ("金融", "高"),
        "web3": ("Web3", "高"),
        "crypto": ("加密", "高"),
        "ai": ("AI", "高"),
        "software": ("软件", "高"),
        "education": ("教育", "中高"),
        "hiring": ("招聘", "中高"),
        "ecommerce": ("电商", "中高"),
        "tools": ("工具", "中高"),
        "vertical": ("垂直社群", "中高"),
        "news": ("新闻资讯", "中"),
        "gossip": ("吃瓜", "中低"),
        "fun": ("搞笑", "中低"),
        "entertainment": ("娱乐", "中低"),
        "movies": ("影视", "低"),
        "anime": ("动漫", "低"),
        "adult": ("成人", "高风险"),
        "general": ("通用", "中"),
    }

    def _channel_quality_lines(self, channel_id: str) -> list[str]:
        """Latest channel assessment as a 1-2 line quality signal block.

        Returned lines are appended right under the channel title in the
        placement configurator so advertisers see traffic / category / risk
        before they commit a budget. No assessment yet → empty list.
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT category, median_24h_views, subscribers, risk_level, score
                FROM channel_pricing_assessments
                WHERE channel_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
        if not row:
            return []
        # Line 1: traffic snapshot
        traffic_parts: list[str] = []
        if row["subscribers"]:
            traffic_parts.append(f"👥 订阅 {int(row['subscribers']):,}")
        if row["median_24h_views"]:
            traffic_parts.append(f"📈 24h 中位浏览 {int(row['median_24h_views']):,}")
        # Line 2: category + business value + risk
        category_label, value_label = self.CATEGORY_VALUE.get(
            (row["category"] or "general").lower(),
            (row["category"] or "未分类", "未评估"),
        )
        risk_emoji = {"normal": "🟢", "watch": "🟡", "high": "🟠", "blocked": "🔴"}.get(row["risk_level"], "⚪️")
        category_line = f"🏷 {category_label} (商业价值 {value_label}) · {risk_emoji} 风控 {row['risk_level']}"

        result: list[str] = []
        if traffic_parts:
            result.append(" · ".join(traffic_parts))
        result.append(category_line)
        return result

    def _placement_cost_breakdown(self, channel: dict[str, Any], payload: dict[str, Any]) -> list[str]:
        """One-shot cost breakdown for the placement confirm panel.

        Shows: per-delivery base / pin multiplier / period multiplier / total,
        plus the publisher-net / platform-fee split so advertisers know how
        the cents are routed.
        """
        slot_type = payload.get("slot_type")
        if not slot_type:
            return ["报价待计算 — 先选展示形态。"]
        normalized = self.channels.normalize_slot_type(slot_type)
        base_unit = self._slot_price_cents(payload["channel_id"], normalized)
        pin_on = bool(payload.get("pin")) and normalized in PINNABLE_PLACEMENT_SLOTS
        pin_unit = base_unit * 2 if pin_on else base_unit
        period_cfg = PLACEMENT_PERIODS.get(payload.get("period") or "once", PLACEMENT_PERIODS["once"])
        deliveries = int(period_cfg["deliveries"])
        discount_bps = int(period_cfg["discount_bps"])
        per_delivery = max(1, round(pin_unit * discount_bps / 10000))
        total = per_delivery * deliveries

        with self.db.transaction() as conn:
            cfg = conn.execute(
                "SELECT service_fee_bps FROM channel_configs WHERE channel_id = ?",
                (payload["channel_id"],),
            ).fetchone()
        fee_bps = int(cfg["service_fee_bps"]) if cfg else self.settings.default_service_fee_bps
        platform_fee = (total * fee_bps) // 10_000
        publisher_net = total - platform_fee

        lines = ["📊 报价拆解"]
        lines.append(f"• 基准 USD {cents_to_money(base_unit)} / 次")
        if pin_on:
            lines.append(f"• 置顶加价 ×2 → USD {cents_to_money(pin_unit)} / 次")
        if discount_bps != 10_000:
            discount_pct = discount_bps / 100  # bps→%
            lines.append(f"• {period_cfg['label']}: {deliveries} 次 × {discount_pct:.0f}% 折扣")
        lines.append(f"• 单次结算 USD {cents_to_money(per_delivery)} × {deliveries} 次 = USD {cents_to_money(total)}")
        if fee_bps > 0:
            lines.append(f"💼 频道主净收 USD {cents_to_money(publisher_net)}｜平台服务费 USD {cents_to_money(platform_fee)} ({fee_bps/100:.1f}%)")
        else:
            lines.append(f"💼 频道主净收 USD {cents_to_money(publisher_net)}｜平台服务费 0% (推广按钮 / 高级订阅)")
        lines.append("")
        return lines

    def _placement_keyboard(self, panel: str, payload: dict[str, Any], creatives: list[Any] | None = None) -> list[list[dict[str, str]]]:
        if panel == "display":
            selected = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            prices = self._placement_slot_prices(payload["channel_id"]) if payload.get("channel_id") else {}
            buttons = []
            for slot_type in PLACEMENT_SLOT_TYPES:
                prefix = "✅ " if selected == slot_type else ""
                price_cents = prices.get(slot_type)
                price_label = f" · USD {cents_to_money(price_cents)}" if price_cents else ""
                buttons.append({"text": f"{prefix}{self._slot_label(slot_type)}{price_label}", "callback_data": f"place:slot:{slot_type}"})
            keyboard = self._button_grid(buttons, 2)
            pin_text = "📌 置顶：开启 (×2)" if payload.get("pin") else "📌 置顶：关闭"
            keyboard.append([{"text": pin_text, "callback_data": "place:pin"}])
            keyboard.append([{"text": "⬅️ 返回配置", "callback_data": "place:home"}])
            return keyboard

        if panel == "schedule":
            current = payload.get("period") or "once"
            period_buttons = [
                {"text": f"{'✅ ' if current == key else ''}{value['label']}", "callback_data": f"place:period:{key}"}
                for key, value in PLACEMENT_PERIODS.items()
            ]
            keyboard = self._button_grid(period_buttons, 2)
            keyboard.append([{"text": "🕒 发布时间", "callback_data": "place:time"}, {"text": "📅 开始日期", "callback_data": "place:time"}])
            keyboard.append([{"text": "⬅️ 返回配置", "callback_data": "place:home"}])
            return keyboard

        if panel == "creative":
            keyboard: list[list[dict[str, str]]] = []
            creatives = creatives or []
            slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            new_target = slot_type if slot_type in PLACEMENT_SLOT_TYPES else "auto"
            selected_id = payload.get("material_id")
            for index, creative in enumerate(creatives):
                preview_text = (creative["light_short_text"] if slot_type == "light_tail" else creative["text"]) or creative["text"] or ""
                label = self._short_title(preview_text.replace("\n", " "), 18)
                marker = "✅" if creative["id"] == selected_id else "📄"
                keyboard.append(
                    [
                        {"text": f"{marker} {label}", "callback_data": f"place:pick:{index}"},
                        {"text": "🗑 归档", "callback_data": f"place:archive:{index}"},
                    ]
                )
            if new_target == "auto":
                keyboard.extend(
                    [
                        [{"text": "➕ 创建文字插播素材", "callback_data": "place:new:light_tail"}],
                        [{"text": "➕ 创建标准插播素材", "callback_data": "place:new:standard_card"}],
                        [{"text": "➕ 创建定制插播素材", "callback_data": "place:new:strong_post"}],
                    ]
                )
            else:
                keyboard.append(
                    [{"text": f"➕ 新建{self._slot_name(new_target)}素材", "callback_data": f"place:new:{new_target}"}]
                )
            keyboard.append([{"text": "⬅️ 返回配置", "callback_data": "place:home"}])
            return keyboard

        if panel == "confirm":
            missing_panel = self._placement_missing_panel(payload)
            if missing_panel:
                target_text = {"display": "🧩 选择展示", "creative": "📁 选择素材"}.get(missing_panel, "继续配置")
                return [[{"text": target_text, "callback_data": f"place:{missing_panel}"}], [{"text": "⬅️ 返回配置", "callback_data": "place:home"}]]
            return [
                [{"text": "✅ 确认投放", "callback_data": "place:submit"}],
                [{"text": "修改配置", "callback_data": "place:home"}, {"text": "保存草稿", "callback_data": "place:draft"}],
            ]

        return [
            [{"text": "🧩 展示设置", "callback_data": "place:display"}, {"text": "⏱ 发布设置", "callback_data": "place:schedule"}],
            [{"text": "📁 广告素材", "callback_data": "place:creative"}, {"text": "✅ 费用确认", "callback_data": "place:confirm"}],
            [{"text": "🏠 工作台", "callback_data": "menu:home"}],
        ]

    PLACEMENT_CREATIVE_KEYS = (
        "material_id",
        "creative_text",
        "target_url",
        "button_text",
        "light_short_text",
    )

    def _swap_placement_creative_draft(
        self,
        payload: dict[str, Any],
        previous_slot: str,
        next_slot: str,
    ) -> None:
        """Save the outgoing slot's creative draft and restore the incoming one.

        Why: switching display format used to clear all creative inputs, so a
        user comparing prices between 文字/标准/定制 lost their work. Now each
        slot keeps its own draft bucket; flipping back restores it.
        """
        drafts = payload.setdefault("_slot_drafts", {})
        if previous_slot:
            drafts[previous_slot] = {
                key: payload.get(key) for key in self.PLACEMENT_CREATIVE_KEYS
            }
        for key in self.PLACEMENT_CREATIVE_KEYS:
            payload.pop(key, None)
        restored = drafts.get(next_slot) or {}
        for key in self.PLACEMENT_CREATIVE_KEYS:
            value = restored.get(key)
            if value:
                payload[key] = value

    def _placement_missing_panel(self, payload: dict[str, Any]) -> str | None:
        if not payload.get("slot_type"):
            return "display"
        if not payload.get("material_id"):
            return "creative"
        return None

    def _placement_display_label(self, payload: dict[str, Any]) -> str:
        slot_type = payload.get("slot_type")
        if not slot_type:
            return "未选择"
        label = self._slot_name(slot_type)
        if payload.get("pin") and self.channels.normalize_slot_type(slot_type) in PINNABLE_PLACEMENT_SLOTS:
            label += " + 置顶"
        return label

    def _placement_period_label(self, payload: dict[str, Any]) -> str:
        period = PLACEMENT_PERIODS.get(payload.get("period") or "once", PLACEMENT_PERIODS["once"])
        return str(period["label"])

    def _placement_creative_label(self, payload: dict[str, Any]) -> str:
        text = payload.get("light_short_text") or payload.get("creative_text")
        return self._short_title(text.replace("\n", " "), 18) if text else "未选择"

    def _placement_cost_label(self, payload: dict[str, Any]) -> str:
        if not payload.get("slot_type"):
            return "待计算"
        quote = self._placement_quote(payload)
        return f"预计 USD {cents_to_money(quote['total_cents'])}"

    def _placement_next_step(self, payload: dict[str, Any]) -> str:
        if not payload.get("slot_type"):
            return "选择展示设置"
        if not payload.get("creative_text") or not payload.get("target_url"):
            return "选择广告素材"
        return "确认费用"

    def _placement_quote(self, payload: dict[str, Any]) -> dict[str, int]:
        base_unit = self._slot_price_cents(payload["channel_id"], payload["slot_type"])
        if payload.get("pin") and self.channels.normalize_slot_type(payload["slot_type"]) in PINNABLE_PLACEMENT_SLOTS:
            base_unit *= 2
        period = PLACEMENT_PERIODS.get(payload.get("period") or "once", PLACEMENT_PERIODS["once"])
        deliveries = int(period["deliveries"])
        unit_cents = max(1, round(base_unit * int(period["discount_bps"]) / 10000))
        return {
            "base_unit_cents": base_unit,
            "unit_cents": unit_cents,
            "total_cents": unit_cents * deliveries,
            "deliveries": deliveries,
        }

    def _send_advertiser_library(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            creatives = conn.execute(
                """
                SELECT cr.*, ca.name AS campaign_name
                FROM creatives cr
                JOIN campaigns ca ON ca.id = cr.campaign_id
                WHERE ca.advertiser_account_id = ?
                ORDER BY cr.updated_at DESC, cr.created_at DESC
                LIMIT 5
                """,
                (account["id"],),
            ).fetchall()
        lines = ["🗂 广告库", ""]
        if creatives:
            for index, creative in enumerate(creatives, start=1):
                number = self.ORDER_LIST_NUMBERS[index - 1] if index <= len(self.ORDER_LIST_NUMBERS) else f"{index}."
                text = (creative["text"] or "").replace("\n", " ")
                if len(text) > 28:
                    text = text[:28] + "..."
                lines.append(f"{number} {text}")
                lines.append(
                    f"   {self._creative_status_label(creative['status'])} · {self._slot_name(creative['format_type'])} · {creative['button_text']}"
                )
        else:
            lines.extend(
                [
                    "还没有可复用广告。",
                    "先从频道里的“频道招商”进入，创建第一条插播。",
                ]
            )
        keyboard: list[list[dict[str, str]]] = []
        if creatives:
            edit_buttons = [
                {
                    "text": f"✏️ {self.ORDER_LIST_NUMBERS[i] if i < len(self.ORDER_LIST_NUMBERS) else str(i + 1)}",
                    "callback_data": f"advertiser:material:edit:{creative['id']}",
                }
                for i, creative in enumerate(creatives)
            ]
            keyboard.append(edit_buttons)
            batch_buttons = [
                {
                    "text": f"📡 {self.ORDER_LIST_NUMBERS[i] if i < len(self.ORDER_LIST_NUMBERS) else str(i + 1)}",
                    "callback_data": f"advertiser:batch:start:{creative['id']}",
                }
                for i, creative in enumerate(creatives)
            ]
            keyboard.append(batch_buttons)
        keyboard.append([
            {"text": "➕ 新建素材", "callback_data": "advertiser:material:new"},
            {"text": "🧾 去频道投放", "callback_data": "advertiser:order_help"},
        ])
        keyboard.append(
            [
                {"text": "📋 投放订单", "callback_data": "advertiser:orders"},
                {"text": "💰 广告钱包", "callback_data": "advertiser:balance"},
            ]
        )
        keyboard.append(
            [
                {"text": "💵 定价规则", "callback_data": "publisher:pricing"},
                {"text": "🏠 主菜单", "callback_data": "menu:home"},
            ]
        )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    MATERIAL_EDIT_FIELDS: dict[str, dict[str, str]] = {
        "text": {"label": "📝 文案", "prompt": "请发送新的广告文案。"},
        "target_url": {
            "label": "🔗 目标链接",
            "prompt": "请发送新的目标链接，必须以 http:// 或 https:// 开头。",
        },
        "button_text": {"label": "🔘 按钮文案", "prompt": "请发送新的按钮文案。"},
        "light_short_text": {
            "label": "✨ 短入口",
            "prompt": "请发送新的短入口（2-15 个字，仅文字插播）。",
        },
    }

    def _send_material_edit_panel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        material_id: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        try:
            material = self.materials.get_material(
                material_id, advertiser_telegram_user_id=user_id
            )
        except NotFound:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 素材不存在或不属于你。",
                inline_keyboard=[
                    [{"text": "🗂 广告库", "callback_data": "advertiser:library"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return
        if material["archived_at"]:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="ℹ️ 已归档的素材无法编辑，请回到广告库新建一条。",
                inline_keyboard=[
                    [{"text": "🗂 广告库", "callback_data": "advertiser:library"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return

        # 清掉残留的 material_edit 对话(用户切到别的素材时不要污染上下文)
        self._clear_material_edit_state(chat_id)

        text_preview = (material["text"] or "").replace("\n", " ")
        if len(text_preview) > 60:
            text_preview = text_preview[:60] + "..."
        body_lines = [
            "✏️ 编辑素材",
            "",
            f"形态：{self._slot_name(material['format_type'])}",
            f"文案：{text_preview or '—'}",
            f"链接：{material['target_url'] or '—'}",
            f"按钮文案：{material['button_text']}",
        ]
        if material["format_type"] == "light_tail":
            body_lines.append(f"短入口：{material['light_short_text'] or '—'}")
        body_lines.extend([
            "",
            "选一个字段修改。已发布订单的展示快照不变，仅影响后续投放。",
        ])

        field_keys = ["text", "target_url", "button_text"]
        if material["format_type"] == "light_tail":
            field_keys.append("light_short_text")
        field_buttons = [
            {
                "text": self.MATERIAL_EDIT_FIELDS[key]["label"],
                "callback_data": f"advertiser:material:field:{material_id}:{key}",
            }
            for key in field_keys
        ]
        keyboard = self._button_grid(field_buttons, 2)
        keyboard.append([{"text": "🗂 返回广告库", "callback_data": "advertiser:library"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(body_lines),
            inline_keyboard=keyboard,
        )

    def _begin_material_field_edit(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        material_id: str,
        field: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        if field not in self.MATERIAL_EDIT_FIELDS:
            self._send_material_edit_panel(chat_id, user, material_id, source_message)
            return
        user_id = user.get("id") or chat_id
        try:
            material = self.materials.get_material(
                material_id, advertiser_telegram_user_id=user_id
            )
        except NotFound:
            self._send_material_edit_panel(chat_id, user, material_id, source_message)
            return
        if material["archived_at"]:
            self._send_material_edit_panel(chat_id, user, material_id, source_message)
            return
        if field == "light_short_text" and material["format_type"] != "light_tail":
            self._send_material_edit_panel(chat_id, user, material_id, source_message)
            return

        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(
                conn, user_id, "advertiser", self._display_name(user)
            )
            self._set_conversation_conn(
                conn,
                chat_id,
                account["id"],
                "material_edit",
                field,
                {"material_id": material_id, "field": field},
            )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=self.MATERIAL_EDIT_FIELDS[field]["prompt"],
            inline_keyboard=[
                [{"text": "↩️ 取消", "callback_data": f"advertiser:material:edit:{material_id}"}],
            ],
        )

    def _handle_material_edit_message(
        self,
        message: dict[str, Any],
        state: dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        if not chat_id:
            return {"handled": False, "reason": "missing_chat"}
        payload = json.loads(state["payload_json"] or "{}")
        material_id = payload.get("material_id")
        field = state["step"]
        if not material_id or field not in self.MATERIAL_EDIT_FIELDS:
            self._clear_material_edit_state(chat_id)
            return {"handled": True, "type": "material_edit_invalid_state"}
        clean_text = (text or "").strip()
        if not clean_text:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="内容不能为空，请重新发送或点击取消。",
                inline_keyboard=[[{"text": "↩️ 取消", "callback_data": f"advertiser:material:edit:{material_id}"}]],
            )
            return {"handled": True, "type": "material_edit_empty"}
        user_id = user.get("id") or chat_id
        kwargs: dict[str, Any] = {field: clean_text}
        try:
            self.materials.update_material(
                material_id,
                advertiser_telegram_user_id=user_id,
                **kwargs,
            )
        except (InvalidState, NotFound) as exc:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text=f"⚠️ 保存失败：{exc}",
                inline_keyboard=[[{"text": "↩️ 取消", "callback_data": f"advertiser:material:edit:{material_id}"}]],
            )
            return {"handled": True, "type": "material_edit_failed", "error": str(exc)}
        self._clear_material_edit_state(chat_id)
        self._send_material_edit_panel(chat_id, user, material_id, None)
        return {"handled": True, "type": "material_edit_saved", "material_id": material_id, "field": field}

    def _clear_material_edit_state(self, chat_id: str | int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT flow FROM bot_conversation_states WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
            if row and row["flow"] == "material_edit":
                conn.execute(
                    "DELETE FROM bot_conversation_states WHERE chat_id = ?",
                    (str(chat_id),),
                )

    def _send_material_format_picker(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        text = (
            "➕ 新建素材\n\n"
            "选择形态。素材会保存到你的广告库，下单时可重复挑选。\n"
            "• 文字插播：频道帖底部短入口，点击进入详情\n"
            "• 标准插播：图文卡片，平台模板\n"
            "• 定制插播：广告主自由排版"
        )
        keyboard = [
            [{"text": "✍️ 文字插播", "callback_data": "advertiser:material:new:light_tail"}],
            [{"text": "🧾 标准插播", "callback_data": "advertiser:material:new:standard_card"}],
            [{"text": "🎨 定制插播", "callback_data": "advertiser:material:new:strong_post"}],
            [{"text": "🗂 返回广告库", "callback_data": "advertiser:library"}],
        ]
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
            inline_keyboard=keyboard,
        )

    def _begin_material_create(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        format_type: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        if format_type not in MaterialService.SUPPORTED_FORMATS:
            self._send_material_format_picker(chat_id, user, source_message)
            return
        user_id = user.get("id") or chat_id
        first_step = "light_short_text" if format_type == "light_tail" else "creative_text"
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(
                conn, user_id, "advertiser", self._display_name(user)
            )
            self._set_conversation_conn(
                conn,
                chat_id,
                account["id"],
                "material_create",
                first_step,
                {"format_type": format_type},
            )
        if format_type == "light_tail":
            prompt = (
                "✍️ 新建文字插播\n\n"
                "短入口最多 15 个字，会显示在频道帖底部。\n"
                "请发送短入口。"
            )
        else:
            label = self._slot_name(format_type)
            prompt = (
                f"➕ 新建{label}素材\n\n"
                "请发送广告文案（4-800 字）。下一步会要链接和按钮文案。"
            )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=prompt,
            inline_keyboard=[
                [{"text": "↩️ 取消", "callback_data": "advertiser:material:new"}],
                [{"text": "🗂 返回广告库", "callback_data": "advertiser:library"}],
            ],
        )

    def _handle_material_create_message(
        self,
        message: dict[str, Any],
        state: dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        if not chat_id:
            return {"handled": False, "reason": "missing_chat"}
        clean_text = (text or "").strip()
        payload = json.loads(state["payload_json"] or "{}")
        format_type = payload.get("format_type")
        step = state["step"]
        cancel_keyboard = [[{"text": "🗂 返回广告库", "callback_data": "advertiser:library"}]]

        if not clean_text:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="请发送文字内容，或点击返回广告库。",
                inline_keyboard=cancel_keyboard,
            )
            return {"handled": True, "type": "material_create_text_required"}

        if step == "light_short_text":
            if len(clean_text) < 2 or len(clean_text) > 15:
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text="文字插播短入口需要 2-15 个字。",
                    inline_keyboard=cancel_keyboard,
                )
                return {"handled": True, "type": "material_create_invalid_short"}
            payload["light_short_text"] = clean_text
            payload["button_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "material_create", "light_detail_text", payload)
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="✅ 短入口已保存\n\n请发送完整广告详情（4-1000 字），用户点击短入口后会看到这段。",
                inline_keyboard=cancel_keyboard,
            )
            return {"handled": True, "type": "material_create_short_saved"}

        if step == "light_detail_text":
            if len(clean_text) < 4 or len(clean_text) > 1000:
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text="广告详情需要 4-1000 个字。",
                    inline_keyboard=cancel_keyboard,
                )
                return {"handled": True, "type": "material_create_invalid_detail"}
            payload["creative_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "material_create", "target_url", payload)
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="请发送广告目标链接，必须以 http:// 或 https:// 开头。",
                inline_keyboard=cancel_keyboard,
            )
            return {"handled": True, "type": "material_create_detail_saved"}

        if step == "creative_text":
            if len(clean_text) < 4 or len(clean_text) > 800:
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text="广告文案需要 4-800 个字。",
                    inline_keyboard=cancel_keyboard,
                )
                return {"handled": True, "type": "material_create_invalid_text"}
            payload["creative_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "material_create", "target_url", payload)
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="请发送广告目标链接，必须以 http:// 或 https:// 开头。",
                inline_keyboard=cancel_keyboard,
            )
            return {"handled": True, "type": "material_create_text_saved"}

        if step == "target_url":
            if not (clean_text.startswith("https://") or clean_text.startswith("http://")):
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text="链接格式不对。请发送以 http:// 或 https:// 开头的目标链接。",
                    inline_keyboard=cancel_keyboard,
                )
                return {"handled": True, "type": "material_create_invalid_url"}
            user_id = user.get("id") or chat_id
            try:
                material = self.materials.create_material(
                    advertiser_telegram_user_id=user_id,
                    format_type=format_type,
                    text=payload.get("creative_text") or "",
                    target_url=clean_text,
                    button_text=payload.get("button_text") or "查看详情",
                    light_short_text=payload.get("light_short_text"),
                    display_name=self._display_name(user) or None,
                )
            except (InvalidState, NotFound) as exc:
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text=f"⚠️ 素材保存失败：{exc}",
                    inline_keyboard=cancel_keyboard,
                )
                return {"handled": True, "type": "material_create_failed", "error": str(exc)}
            self._clear_material_create_state(chat_id)
            self.gateway.send_private_message(
                chat_id=chat_id,
                text=f"✅ 素材已保存（ID {material['id']}）",
                inline_keyboard=[[{"text": "🗂 广告库", "callback_data": "advertiser:library"}]],
            )
            self._send_advertiser_library(chat_id, user, None)
            return {"handled": True, "type": "material_create_saved", "material_id": material["id"]}

        return {"handled": False, "reason": "unknown_material_create_step"}

    BATCH_DEFAULT_BUDGET_CENTS = 1000  # USD 10 per channel
    BATCH_CANDIDATE_LIMIT = 5

    def _begin_batch_orders(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        material_id: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        try:
            material = self.materials.get_material(
                material_id, advertiser_telegram_user_id=user_id
            )
        except NotFound:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 素材不存在或不属于你。",
                inline_keyboard=[
                    [{"text": "🗂 广告库", "callback_data": "advertiser:library"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return
        if material["archived_at"]:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="ℹ️ 已归档的素材无法批量投放，请回到广告库选择活跃素材。",
                inline_keyboard=[
                    [{"text": "🗂 广告库", "callback_data": "advertiser:library"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return
        try:
            candidates = self.advertisers.discover_channels(
                advertiser_telegram_user_id=user_id,
                slot_type=material["format_type"],
                limit=self.BATCH_CANDIDATE_LIMIT,
            )
        except Exception as exc:
            logger.warning("batch_discover_failed user=%s error=%s", user_id, exc)
            candidates = []

        if not candidates:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=(
                    "📡 批量投放\n\n"
                    "暂无符合条件的频道（要求该形态 standard 报价已生效）。\n"
                    "也可以从频道帖底部的「📣 频道招商」按钮进入单频道投放。"
                ),
                inline_keyboard=[
                    [{"text": "🗂 返回广告库", "callback_data": "advertiser:library"}],
                ],
            )
            return

        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(
                conn, user_id, "advertiser", self._display_name(user)
            )
            payload = {
                "material_id": material_id,
                "slot_type": material["format_type"],
                "budget_cents": self.BATCH_DEFAULT_BUDGET_CENTS,
                "selected_channel_ids": [],
                "candidates": [
                    {
                        "channel_id": c["channel_id"],
                        "title": c["title"],
                        "category": c.get("category"),
                        "risk_level": c.get("risk_level"),
                        "score": c.get("score"),
                        "subscribers": c.get("subscribers"),
                        "list_price_cents": c.get("list_price_cents"),
                    }
                    for c in candidates
                ],
            }
            self._set_conversation_conn(
                conn,
                chat_id,
                account["id"],
                "batch_orders",
                "select_channels",
                payload,
            )
        self._send_batch_orders_panel(chat_id, user, source_message)

    def _send_batch_orders_panel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        state = self._get_conversation(chat_id)
        if not state or state["flow"] != "batch_orders":
            self._send_advertiser_library(chat_id, user, source_message)
            return
        payload = json.loads(state["payload_json"] or "{}")
        candidates = payload.get("candidates") or []
        selected = set(payload.get("selected_channel_ids") or [])
        slot_type = payload.get("slot_type") or "standard_card"
        budget_cents = int(payload.get("budget_cents") or self.BATCH_DEFAULT_BUDGET_CENTS)
        slot_label = self._slot_name(slot_type)

        lines = [
            "📡 批量投放",
            "",
            f"形态：{slot_label}",
            f"单频道预算：USD {cents_to_money(budget_cents)}",
            f"已选：{len(selected)} 个频道",
            "",
            "勾选要投放的频道：",
        ]
        toggle_buttons: list[dict[str, str]] = []
        for index, channel in enumerate(candidates):
            number = self.DISCOVER_LIST_NUMBERS[index] if index < len(self.DISCOVER_LIST_NUMBERS) else f"{index + 1}."
            mark = "✅" if channel["channel_id"] in selected else "☐"
            risk_emoji = self.DISCOVER_RISK_EMOJI.get(channel.get("risk_level"), "⚪️")
            category_label = self.CATEGORY_VALUE.get(channel.get("category") or "general", ("通用", ""))[0]
            price = cents_to_money(int(channel.get("list_price_cents") or 0))
            lines.append(
                f"{mark} {number} {channel['title']}｜🏷 {category_label}｜{risk_emoji}"
                f"｜⭐ {channel.get('score') or 0}｜USD {price}"
            )
            toggle_buttons.append({
                "text": f"{mark} {number}",
                "callback_data": f"advertiser:batch:toggle:{channel['channel_id']}",
            })

        keyboard = self._button_grid(toggle_buttons, 5)
        keyboard.append([{"text": "💵 改单频道预算", "callback_data": "advertiser:batch:budget"}])
        if selected:
            estimated = budget_cents * len(selected)
            lines.extend([
                "",
                f"💵 预估总冻结：USD {cents_to_money(estimated)}",
            ])
            keyboard.append([
                {
                    "text": f"🚀 批量投放 ({len(selected)})",
                    "callback_data": "advertiser:batch:submit",
                },
            ])
        keyboard.append([{"text": "↩️ 取消", "callback_data": "advertiser:batch:cancel"}])

        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _toggle_batch_channel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_id: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        state = self._get_conversation(chat_id)
        if not state or state["flow"] != "batch_orders":
            self._send_advertiser_library(chat_id, user, source_message)
            return
        payload = json.loads(state["payload_json"] or "{}")
        candidate_ids = {c["channel_id"] for c in (payload.get("candidates") or [])}
        if channel_id not in candidate_ids:
            self._send_batch_orders_panel(chat_id, user, source_message)
            return
        selected = list(payload.get("selected_channel_ids") or [])
        if channel_id in selected:
            selected = [c for c in selected if c != channel_id]
        else:
            selected.append(channel_id)
        payload["selected_channel_ids"] = selected
        with self.db.transaction() as conn:
            self._set_conversation_conn(
                conn,
                chat_id,
                state["account_id"],
                "batch_orders",
                state["step"],
                payload,
            )
        self._send_batch_orders_panel(chat_id, user, source_message)

    def _begin_batch_budget_input(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        state = self._get_conversation(chat_id)
        if not state or state["flow"] != "batch_orders":
            self._send_advertiser_library(chat_id, user, source_message)
            return
        payload = json.loads(state["payload_json"] or "{}")
        with self.db.transaction() as conn:
            self._set_conversation_conn(
                conn,
                chat_id,
                state["account_id"],
                "batch_orders",
                "budget_input",
                payload,
            )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "💵 单频道预算\n\n"
                "请发送数字（USD），会用于每一个被勾选的频道。\n"
                "例如：10 → 每个频道冻结 USD 10。"
            ),
            inline_keyboard=[
                [{"text": "↩️ 取消", "callback_data": "advertiser:batch:cancel"}],
            ],
        )

    def _handle_batch_orders_message(
        self,
        message: dict[str, Any],
        state: dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        if not chat_id:
            return {"handled": False, "reason": "missing_chat"}
        if state["step"] != "budget_input":
            return {"handled": False, "reason": "unsupported_batch_step"}
        clean_text = (text or "").strip()
        try:
            budget_cents = money_to_cents(clean_text)
        except Exception:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="预算格式不对，请发送正数（最多两位小数）。",
                inline_keyboard=[[{"text": "↩️ 取消", "callback_data": "advertiser:batch:cancel"}]],
            )
            return {"handled": True, "type": "batch_budget_invalid"}
        if budget_cents <= 0:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="预算需要是大于 0 的金额。",
                inline_keyboard=[[{"text": "↩️ 取消", "callback_data": "advertiser:batch:cancel"}]],
            )
            return {"handled": True, "type": "batch_budget_invalid"}
        payload = json.loads(state["payload_json"] or "{}")
        payload["budget_cents"] = budget_cents
        with self.db.transaction() as conn:
            self._set_conversation_conn(
                conn,
                chat_id,
                state["account_id"],
                "batch_orders",
                "select_channels",
                payload,
            )
        self._send_batch_orders_panel(chat_id, user, None)
        return {"handled": True, "type": "batch_budget_saved", "budget_cents": budget_cents}

    def _submit_batch_orders(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self._get_conversation(chat_id)
        if not state or state["flow"] != "batch_orders":
            self._send_advertiser_library(chat_id, user, source_message)
            return {"handled": True, "type": "callback_batch_no_state"}
        payload = json.loads(state["payload_json"] or "{}")
        material_id = payload.get("material_id")
        slot_type = payload.get("slot_type") or "standard_card"
        budget_cents = int(payload.get("budget_cents") or 0)
        candidate_lookup = {c["channel_id"]: c for c in (payload.get("candidates") or [])}
        selected_ids = list(payload.get("selected_channel_ids") or [])
        if not selected_ids or budget_cents <= 0:
            self._send_batch_orders_panel(chat_id, user, source_message)
            return {"handled": True, "type": "callback_batch_incomplete"}

        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            tokens = []
            for channel_id in selected_ids:
                row = conn.execute(
                    "SELECT ref_token FROM channels WHERE id = ?", (channel_id,)
                ).fetchone()
                if row:
                    tokens.append(row["ref_token"])
        if not tokens:
            self._send_batch_orders_panel(chat_id, user, source_message)
            return {"handled": True, "type": "callback_batch_no_tokens"}

        try:
            outcome = self.advertisers.create_batch_orders(
                advertiser_telegram_user_id=user_id,
                channel_tokens=tokens,
                slot_type=slot_type,
                material_id=material_id,
                budget_cents=budget_cents,
            )
        except (InvalidState, NotFound, ChaboError) as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 批量投放失败：{exc}",
                inline_keyboard=[
                    [{"text": "📡 返回批量页", "callback_data": f"advertiser:batch:start:{material_id}"}],
                    [{"text": "🗂 广告库", "callback_data": "advertiser:library"}],
                ],
            )
            return {"handled": True, "type": "callback_batch_failed", "error": str(exc)}

        self._clear_batch_orders_state(chat_id)
        with self.db.transaction() as conn:
            placeholders = ",".join(["?"] * len(tokens))
            title_by_token = {
                row["ref_token"]: row["title"]
                for row in conn.execute(
                    f"SELECT ref_token, title FROM channels WHERE ref_token IN ({placeholders})",
                    tokens,
                ).fetchall()
            }
        lines = [
            f"🚀 批量投放完成（{outcome['created_count']} 成功 / {outcome['failed_count']} 失败）",
            "",
        ]
        for item in outcome["results"]:
            channel_label = title_by_token.get(item["channel_token"], item["channel_token"])
            if item["ok"]:
                lines.append(f"✅ {channel_label}｜订单 {item['order_id']}")
            else:
                lines.append(f"❌ {channel_label}｜{item['error']}")
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[
                [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                [{"text": "🗂 广告库", "callback_data": "advertiser:library"}],
                [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ],
        )
        return {
            "handled": True,
            "type": "callback_batch_submitted",
            "created_count": outcome["created_count"],
            "failed_count": outcome["failed_count"],
        }

    def _clear_batch_orders_state(self, chat_id: str | int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT flow FROM bot_conversation_states WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
            if row and row["flow"] == "batch_orders":
                conn.execute(
                    "DELETE FROM bot_conversation_states WHERE chat_id = ?",
                    (str(chat_id),),
                )

    def _clear_material_create_state(self, chat_id: str | int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT flow FROM bot_conversation_states WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
            if row and row["flow"] == "material_create":
                conn.execute(
                    "DELETE FROM bot_conversation_states WHERE chat_id = ?",
                    (str(chat_id),),
                )

    def _send_publisher_menu(
        self,
        chat_id: str | int,
        user: dict[str, Any] | None = None,
        source_message: dict[str, Any] | None = None,
        channels: list[dict[str, Any]] | None = None,
    ) -> None:
        user = user or {}
        user_id = user.get("id") or chat_id
        display_name = self._display_name(user) or None
        channels = channels if channels is not None else self._publisher_channels_for_user(user_id, display_name)
        if channels:
            lines = [
                "📺 频道管理",
                "",
                f"✅ {len(channels)} 个频道资产",
                "选择频道配置插播。",
            ]
            keyboard = [[{"text": f"📺 {channel['title']}", "callback_data": f"pub:channel:{channel['ref_token']}"}] for channel in channels[:8]]
            keyboard.extend(
                [
                    [{"text": "➕ 添加频道", "url": self._add_channel_url()}],
                    [{"text": "🔌 手动接入", "callback_data": "publisher:onboard"}, {"text": "💵 定价规则", "callback_data": "publisher:pricing"}],
                    [{"text": "💸 我的收益", "callback_data": "publisher:earnings"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ]
            )
        else:
            lines = [
                "📺 频道管理",
                "",
                "还没有频道资产。",
                "把插播加为频道管理员即可识别。",
            ]
            keyboard = [
                [{"text": "➕ 添加频道", "url": self._add_channel_url()}],
                [{"text": "🔌 手动接入", "callback_data": "publisher:onboard"}, {"text": "💵 定价规则", "callback_data": "publisher:pricing"}],
                [{"text": "💸 我的收益", "callback_data": "publisher:earnings"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ]
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _send_publisher_channel_dashboard(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            channel = self._sync_channel_profile_conn(conn, self._find_channel(conn, channel_identifier))
            stats = self._channel_dashboard_stats(conn, channel["id"])
        channels = self._publisher_channels_for_user(user_id, self._display_name(user))
        if not any(row["id"] == channel["id"] for row in channels):
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 未确认你是该频道管理员。",
                inline_keyboard=[[{"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}]],
            )
            return
        permission = self._check_channel_permissions(channel["telegram_chat_id"], user_id)
        ready = "✅ 可接广告" if permission["ok"] and stats["enabled_formats"] else "⚠️ 待补权限/形态"
        formats_label = "、".join(self._slot_name(f) for f in stats["enabled_formats"]) or "未开启"
        lines = [
            f"📺 {channel['title']}",
            "",
            f"状态：{ready}",
            f"权限：{self._permission_summary(permission)}",
            f"今日广告：{stats['today_ads']} / {stats['daily_limit']}",
            f"可投形态：{formats_label}",
            f"当前档位：{self._price_band_summary(stats['price_bands'])}",
            f"待确认收益：USD {cents_to_money(stats['pending_earnings_cents'])}",
        ]
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[
                [{"text": "⚙️ 接广告设置", "callback_data": f"pub:approval:{channel['ref_token']}"}, {"text": "💵 价格档位", "callback_data": f"pub:band:{channel['ref_token']}"}],
                [{"text": "🧩 展示形态", "callback_data": f"pub:formats:{channel['ref_token']}"}, {"text": "⏱ 频控时间", "callback_data": f"pub:limit:{channel['ref_token']}"}],
                [{"text": "🪧 自用发布", "callback_data": f"pub:self:{channel['ref_token']}"}, {"text": "📊 数据", "callback_data": f"pub:stats:{channel['ref_token']}"}],
                [{"text": "💸 收益明细", "callback_data": f"pub:earnings:{channel['ref_token']}"}, {"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}],
            ],
        )

    def _channel_dashboard_stats(self, conn: sqlite3.Connection, channel_id: str) -> dict[str, Any]:
        config = conn.execute(
            "SELECT daily_ad_limit FROM channel_configs WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        daily_limit = config["daily_ad_limit"] if config else 3
        today_ads = conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE channel_id = ? AND status IN ('sent', 'confirmed') AND DATE(sent_at) = DATE('now')",
            (channel_id,),
        ).fetchone()["n"]
        policies = conn.execute(
            "SELECT format_type, enabled, owner_price_band FROM channel_ad_format_policies WHERE channel_id = ? ORDER BY format_type",
            (channel_id,),
        ).fetchall()
        enabled_formats = [row["format_type"] for row in policies if row["enabled"]]
        price_bands = [row["owner_price_band"] for row in policies if row["enabled"]] or [row["owner_price_band"] for row in policies]
        pending = conn.execute(
            """
            SELECT COALESCE(SUM(d.publisher_net_cents - d.publisher_reversed_cents), 0) AS total
            FROM deliveries d
            WHERE d.channel_id = ? AND d.status IN ('sent', 'confirmed')
            """,
            (channel_id,),
        ).fetchone()["total"]
        return {
            "daily_limit": daily_limit,
            "today_ads": today_ads,
            "enabled_formats": enabled_formats,
            "price_bands": price_bands,
            "pending_earnings_cents": pending,
        }

    def _price_band_summary(self, bands: list[str]) -> str:
        labels = {"low": "低档", "medium": "中档", "high": "高档", "custom": "自定义"}
        if not bands:
            return "未设置"
        unique = list(dict.fromkeys(bands))
        if len(unique) == 1:
            return labels.get(unique[0], unique[0])
        return "混合（" + " / ".join(labels.get(b, b) for b in unique) + "）"

    def _refresh_publisher_channel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            channel = self._find_channel(conn, channel_identifier)
        self._sync_channel_admins(channel["id"], channel["telegram_chat_id"])
        self._send_publisher_channel_dashboard(chat_id, user, channel["ref_token"], source_message)

    def _send_advertiser_balance(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
        *,
        prefix: str | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        summary = self.ledger.get_wallet_summary(telegram_user_id=user_id)
        body = (
            "💰 广告钱包\n\n"
            f"💵 可用：USD {cents_to_money(summary['available_balance_cents'])}\n"
            f"🔒 冻结：USD {cents_to_money(summary['reserved_balance_cents'])}\n"
            f"📊 已花：USD {cents_to_money(summary['spent_balance_cents'])}"
        )
        text = f"{prefix}\n\n{body}" if prefix else body
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
            inline_keyboard=[
                [{"text": "💳 Stars 充值", "callback_data": "wallet:topup"}],
                [{"text": "🔒 冻结明细", "callback_data": "wallet:reserved"}, {"text": "📜 账单流水", "callback_data": "wallet:statement"}],
                [{"text": "📣 我的广告", "callback_data": "role:advertiser"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ],
        )

    WALLET_TOPUP_PRESETS = (100, 500, 1000, 2000)

    def _send_wallet_topup_picker(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        rate = self.settings.star_credit_cents
        lines = [
            "💳 Stars 充值",
            "",
            f"汇率：1 ⭐ = USD {cents_to_money(rate)} 插播余额",
            "选一个金额，会发一张 Telegram Stars 发票，付款成功即到账。",
        ]
        amount_buttons = [
            {
                "text": f"{stars} ⭐ → USD {cents_to_money(stars * rate)}",
                "callback_data": f"wallet:topup:{stars}",
            }
            for stars in self.WALLET_TOPUP_PRESETS
        ]
        keyboard = self._button_grid(amount_buttons, 2)
        keyboard.append([{"text": "⬅️ 返回钱包", "callback_data": "advertiser:balance"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _trigger_wallet_topup(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        stars_amount: int,
        source_message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_id = user.get("id") or chat_id
        try:
            invoice_response = self.stars_payments.create_balance_topup_invoice(
                telegram_user_id=user_id,
                stars_amount=stars_amount,
                display_name=self._display_name(user),
            )
        except ChaboError as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 无法发起充值\n\n{exc}",
                inline_keyboard=[[{"text": "⬅️ 返回钱包", "callback_data": "advertiser:balance"}]],
            )
            return {"handled": True, "type": "callback_wallet_topup_failed", "error": str(exc)}
        invoice = invoice_response["invoice"]
        try:
            invoice_message_id = self.gateway.send_invoice(
                chat_id=user_id,
                title=invoice["title"],
                description=invoice["description"],
                payload=invoice["payload"],
                currency=invoice["currency"],
                prices=invoice["prices"],
            )
        except TelegramError as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 发票发送失败\n\n{exc}",
                inline_keyboard=[[{"text": "⬅️ 返回钱包", "callback_data": "advertiser:balance"}]],
            )
            return {"handled": True, "type": "callback_wallet_topup_failed", "error": str(exc)}
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                f"💳 已发送 {stars_amount} ⭐ 充值发票\n\n"
                "在 Telegram 内打开发票完成支付，到账后回到广告钱包查看余额。"
            ),
            inline_keyboard=[[{"text": "⬅️ 返回钱包", "callback_data": "advertiser:balance"}]],
        )
        return {
            "handled": True,
            "type": "callback_wallet_topup_invoice_sent",
            "stars_amount": stars_amount,
            "invoice_message_id": invoice_message_id,
        }

    def _send_wallet_reserved(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        rows = self.ledger.list_reserved_orders(telegram_user_id=user_id)
        lines = ["🔒 冻结明细", ""]
        if not rows:
            lines.append("当前没有冻结的预算。")
        else:
            for row in rows:
                title = row["channel_title"] or "（未知频道）"
                lines.append(
                    f"📌 {title}｜{row['currency']} {cents_to_money(row['reserved_cents'])}（订单 {row['order_id']}）"
                )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[
                [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                [{"text": "⬅️ 返回钱包", "callback_data": "advertiser:balance"}],
            ],
        )

    def _send_wallet_statement(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        rows = self.ledger.list_transactions(telegram_user_id=user_id, limit=10)
        lines = ["📜 账单流水（最近 10 条）", ""]
        if not rows:
            lines.append("还没有账务记录。")
        else:
            for row in rows:
                sign = "+" if row["amount_cents"] >= 0 else "-"
                amount = cents_to_money(abs(row["amount_cents"]))
                kind = self._wallet_tx_label(row["type"])
                memo = row["memo"] or ""
                detail = f" — {memo}" if memo else ""
                lines.append(f"{sign} {row['currency']} {amount}｜{kind}{detail}")
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[[{"text": "⬅️ 返回钱包", "callback_data": "advertiser:balance"}]],
        )

    LEDGER_TX_LABELS = {
        "manual_topup": "人工入账",
        "stars_topup": "Stars 充值",
        "budget_reserved": "预算冻结",
        "budget_released": "释放冻结",
        "delivery_charged": "投放扣费",
        "delivery_refunded": "投放退款",
        "advertiser_subscription_charged": "高级服务扣费",
        "publisher_pending_earning": "频道入账（待确认）",
        "publisher_earning_confirmed": "收益确认",
        "publisher_earning_reversed": "收益回滚",
        "publisher_subscription_charged": "频道订阅扣费",
        "publisher_subscription_revenue": "频道订阅收入",
        "advertiser_subscription_revenue": "广告主订阅收入",
        "platform_service_fee": "平台服务费",
        "platform_fee_reversed": "服务费回滚",
    }

    PUBLISHER_TX_TYPES = {
        "publisher_pending_earning",
        "publisher_earning_confirmed",
        "publisher_earning_reversed",
        "publisher_subscription_charged",
    }

    @classmethod
    def _wallet_tx_label(cls, tx_type: str) -> str:
        return cls.LEDGER_TX_LABELS.get(tx_type, tx_type)

    def _send_publisher_earnings(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        summary = self.ledger.get_earnings_summary(telegram_user_id=user_id)
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "💸 我的收益\n\n"
                f"⏳ 待确认：USD {cents_to_money(summary['pending_earnings_cents'])}\n"
                f"✅ 已确认：USD {cents_to_money(summary['confirmed_earnings_cents'])}\n"
                f"💵 可结算：USD {cents_to_money(summary['releasable_earnings_cents'])}"
            ),
            inline_keyboard=[
                [{"text": "📊 频道分布", "callback_data": "earnings:channels"}, {"text": "📜 收益流水", "callback_data": "earnings:statement"}],
                [{"text": "📺 频道管理", "callback_data": "publisher:channels"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ],
        )

    def _send_earnings_channels(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        rows = self.ledger.list_channel_earnings(publisher_telegram_user_id=user_id)
        lines = ["📊 频道分布", ""]
        keyboard: list[list[dict[str, str]]] = []
        if not rows:
            lines.append("还没有可结算的频道。")
        else:
            for row in rows:
                lines.append(
                    f"📺 {row['title']}\n"
                    f"  ⏳ 待确认 USD {cents_to_money(row['pending_cents'])}｜"
                    f"✅ 已确认 USD {cents_to_money(row['confirmed_cents'])}｜"
                    f"📊 平台已收 USD {cents_to_money(row['platform_fee_cents'])}"
                )
                keyboard.append(
                    [{"text": f"📺 {row['title']}", "callback_data": f"pub:channel:{row['ref_token']}"}]
                )
        keyboard.append([{"text": "⬅️ 我的收益", "callback_data": "publisher:earnings"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _send_earnings_statement(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        rows = self.ledger.list_transactions(telegram_user_id=user_id, limit=30)
        rows = [r for r in rows if r["type"] in self.PUBLISHER_TX_TYPES][:10]
        lines = ["📜 收益流水（最近 10 条）", ""]
        if not rows:
            lines.append("还没有收益记录。")
        else:
            for row in rows:
                sign = "+" if row["amount_cents"] >= 0 else "-"
                amount = cents_to_money(abs(row["amount_cents"]))
                kind = self._wallet_tx_label(row["type"])
                memo = row["memo"] or ""
                detail = f" — {memo}" if memo else ""
                lines.append(f"{sign} {row['currency']} {amount}｜{kind}{detail}")
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[[{"text": "⬅️ 我的收益", "callback_data": "publisher:earnings"}]],
        )

    def _handle_conversation_message(self, message: dict[str, Any], text: str) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        user_id = user.get("id") or chat_id
        if not chat_id:
            return {"handled": False, "reason": "no_conversation"}
        state = self._get_conversation(chat_id)
        if not state:
            return {"handled": False, "reason": "no_conversation"}
        if state["flow"] == "timezone_setup":
            return self._handle_timezone_message(message, state, text)
        if state["flow"] == "publisher_onboarding":
            return self._handle_publisher_onboarding_message(message, state)
        if state["flow"] == "placement_config":
            return self._handle_placement_message(message, state, text)
        if state["flow"] == "material_edit":
            return self._handle_material_edit_message(message, state, text)
        if state["flow"] == "material_create":
            return self._handle_material_create_message(message, state, text)
        if state["flow"] == "batch_orders":
            return self._handle_batch_orders_message(message, state, text)
        if state["flow"] == "dispute_open":
            return self._handle_dispute_open_message(message, state, text)
        if state["flow"] != "create_order":
            return {"handled": False, "reason": "unsupported_conversation"}
        if not text:
            self.gateway.send_private_message(chat_id=chat_id, text="请发送文字内容，或点击取消退出当前插播操作。", inline_keyboard=self._cancel_keyboard())
            return {"handled": True, "type": "order_form_text_required"}

        payload = json.loads(state["payload_json"] or "{}")
        step = state["step"]
        clean_text = text.strip()
        if step == "light_short_text":
            if len(clean_text) < 2:
                self.gateway.send_private_message(chat_id=chat_id, text="轻插播短入口太短了，请输入 2-15 个字。", inline_keyboard=self._cancel_keyboard())
                return {"handled": True, "type": "order_form_invalid_light_short_text"}
            if len(clean_text) > 15:
                self.gateway.send_private_message(chat_id=chat_id, text="轻插播短入口最多 15 个字，请重新发送。", inline_keyboard=self._cancel_keyboard())
                return {"handled": True, "type": "order_form_invalid_light_short_text"}
            payload["light_short_text"] = clean_text
            payload["button_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "create_order", "light_detail_text", payload)
            self.gateway.send_private_message(
                chat_id=chat_id,
                text=(
                    "✅ 短入口已保存\n\n"
                    "请发送完整广告详情。\n"
                    "用户点击轻插播后，会在 Bot 里看到这段完整内容。"
                ),
                inline_keyboard=self._cancel_keyboard(),
            )
            return {"handled": True, "type": "order_form_light_short_text_saved"}

        if step == "light_detail_text":
            if len(clean_text) < 4:
                self.gateway.send_private_message(chat_id=chat_id, text="广告详情太短了，请至少输入 4 个字。", inline_keyboard=self._cancel_keyboard())
                return {"handled": True, "type": "order_form_invalid_light_detail"}
            if len(clean_text) > 1000:
                self.gateway.send_private_message(chat_id=chat_id, text="广告详情太长了，请控制在 1000 字以内。", inline_keyboard=self._cancel_keyboard())
                return {"handled": True, "type": "order_form_invalid_light_detail"}
            payload["creative_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "create_order", "target_url", payload)
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="请发送广告目标链接，用户看完完整广告后可继续打开。必须以 http:// 或 https:// 开头。",
                inline_keyboard=self._cancel_keyboard(),
            )
            return {"handled": True, "type": "order_form_light_detail_saved"}

        if step == "creative_text":
            if len(clean_text) < 4:
                self.gateway.send_private_message(chat_id=chat_id, text="广告文案太短了，请至少输入 4 个字。", inline_keyboard=self._cancel_keyboard())
                return {"handled": True, "type": "order_form_invalid_creative"}
            if len(clean_text) > 800:
                self.gateway.send_private_message(chat_id=chat_id, text="广告文案太长了，请控制在 800 字以内。", inline_keyboard=self._cancel_keyboard())
                return {"handled": True, "type": "order_form_invalid_creative"}
            payload["creative_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "create_order", "target_url", payload)
            self.gateway.send_private_message(chat_id=chat_id, text="请发送广告目标链接，必须以 http:// 或 https:// 开头。", inline_keyboard=self._cancel_keyboard())
            return {"handled": True, "type": "order_form_creative_saved"}

        if step == "target_url":
            if not (clean_text.startswith("https://") or clean_text.startswith("http://")):
                self.gateway.send_private_message(chat_id=chat_id, text="链接格式不对。请发送以 http:// 或 https:// 开头的目标链接。", inline_keyboard=self._cancel_keyboard())
                return {"handled": True, "type": "order_form_invalid_url"}
            payload["target_url"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "create_order", "budget", payload)
            self._ask_order_budget(chat_id, payload, None)
            return {"handled": True, "type": "order_form_url_saved"}

        if step == "budget":
            try:
                budget_cents = money_to_cents(clean_text)
            except Exception:
                self.gateway.send_private_message(chat_id=chat_id, text="预算格式不对。请发送数字，例如 20 或 20.50。", inline_keyboard=self._cancel_keyboard())
                return {"handled": True, "type": "order_form_invalid_budget"}
            price = self._slot_price_cents(payload["channel_id"], payload["slot_type"])
            if budget_cents < price:
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text=f"预算低于该广告位单次价格。最低需要 USD {cents_to_money(price)}，请重新输入预算。",
                    inline_keyboard=self._cancel_keyboard(),
                )
                return {"handled": True, "type": "order_form_budget_too_low"}
            try:
                with self.db.transaction() as conn:
                    channel = self.channels.get_channel(conn, payload["channel_id"])
                order = self.orders.create_order(
                    advertiser_telegram_user_id=user_id,
                    channel_token=channel["ref_token"],
                    slot_type=payload["slot_type"],
                    text=payload["creative_text"],
                    target_url=payload["target_url"],
                    button_text=payload.get("button_text", "查看详情"),
                    budget_cents=budget_cents,
                    campaign_name="Bot 自助轻插播广告" if payload["slot_type"] == "light_tail" else "Bot 自助插播广告",
                )
                if self.settings.bot_auto_approve_orders:
                    order = self.orders.approve_order(order["id"])
            except ChaboError as exc:
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text=f"暂时无法创建订单：{exc}\n\n你可以先充值余额，或重新输入预算。",
                    inline_keyboard=[
                        [{"text": "💰 广告钱包", "callback_data": "advertiser:balance"}],
                        [{"text": "❌ 取消", "callback_data": "order:cancel"}],
                    ],
                )
                return {"handled": True, "type": "order_form_create_failed", "error": str(exc)}
            self._clear_conversation(chat_id)
            review_text = "✅ 已自动通过审核\n🚀 已进入排期" if self.settings.bot_auto_approve_orders else "⏳ 等待审核"
            self.gateway.send_private_message(
                chat_id=chat_id,
                text=(
                    "✅ 订单已创建\n\n"
                    f"订单：{order['id']}\n"
                    f"广告位：{self._slot_label(payload['slot_type'])}\n"
                    f"预算：USD {cents_to_money(budget_cents)}\n\n"
                    f"{review_text}"
                ),
                inline_keyboard=[
                    [{"text": "📋 订单", "callback_data": "advertiser:orders"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return {"handled": True, "type": "order_form_order_created", "order_id": order["id"]}

        return {"handled": False, "reason": "unknown_conversation_step"}

    ORDER_LIST_NUMBERS = ("①", "②", "③", "④", "⑤")

    def _send_advertiser_orders(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "advertiser", user.get("first_name") or user.get("username"))
            rows = conn.execute(
                """
                SELECT o.id, o.status, o.budget_cents, o.spent_cents, c.title
                FROM ad_orders o
                JOIN channels c ON c.id = o.channel_id
                WHERE o.advertiser_account_id = ?
                ORDER BY o.created_at DESC
                LIMIT 5
                """,
                (account["id"],),
            ).fetchall()

        # P1-8: aggregate report header so advertiser sees totals at a glance
        try:
            report = self.advertisers.report(user_id)
        except Exception:
            report = None

        lines: list[str] = []
        if report:
            lines.append("📊 投放总览")
            lines.append(
                f"• 订单 {report['orders_count']}｜已发 {report['sent_count']}/{report['deliveries_count']}"
            )
            lines.append(
                f"• 总预算 USD {cents_to_money(report['total_budget_cents'])}"
                f"｜已扣费 USD {cents_to_money(report['charged_cents'])}"
            )
            lines.append(f"• 详情页点击 {report['bot_starts']}")
            if report.get("by_channel") and not report.get("limited"):
                lines.append("")
                lines.append("📺 频道分布")
                for channel_row in report["by_channel"][:5]:
                    lines.append(
                        f"• {channel_row['title']}｜{channel_row['orders_count']} 单｜"
                        f"已扣费 USD {cents_to_money(channel_row['charged_cents'])}"
                    )
            elif report.get("limited"):
                lines.append("")
                lines.append("ℹ️ 频道分布与完整报表需要 Pro / Enterprise 套餐。")
            lines.append("")

        if rows:
            lines.append("📋 最近订单")
            for index, row in enumerate(rows):
                number = self.ORDER_LIST_NUMBERS[index] if index < len(self.ORDER_LIST_NUMBERS) else f"{index + 1}."
                lines.append(
                    f"{number} {row['title']}｜{self._status_label(row['status'])}"
                    f"｜预算 USD {cents_to_money(int(row['budget_cents']))}"
                    f"｜已花 USD {cents_to_money(int(row['spent_cents']))}"
                )
        else:
            lines.append("📋 暂无订单\n\n从频道按钮进入即可创建。")

        keyboard: list[list[dict[str, str]]] = []
        if rows:
            detail_buttons = [
                {
                    "text": f"📄 {self.ORDER_LIST_NUMBERS[i] if i < len(self.ORDER_LIST_NUMBERS) else str(i + 1)}",
                    "callback_data": f"advertiser:order:{row['id']}",
                }
                for i, row in enumerate(rows)
            ]
            keyboard.append(detail_buttons)
        keyboard.append(
            [{"text": "📣 我的广告", "callback_data": "role:advertiser"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}]
        )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _send_advertiser_order_detail(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        order_id: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(
                conn, user_id, "advertiser", user.get("first_name") or user.get("username")
            )
            order = conn.execute(
                """
                SELECT o.*, c.title AS channel_title, s.slot_type
                FROM ad_orders o
                JOIN channels c ON c.id = o.channel_id
                JOIN ad_slots s ON s.id = o.slot_id
                WHERE o.id = ? AND o.advertiser_account_id = ?
                """,
                (order_id, account["id"]),
            ).fetchone()
            if not order:
                self._reply_or_edit(
                    chat_id=chat_id,
                    source_message=source_message,
                    text="⚠️ 订单不存在或不属于你。",
                    inline_keyboard=[
                        [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                        [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                    ],
                )
                return
            deliveries = conn.execute(
                """
                SELECT id, status, scheduled_at, sent_at, charge_cents, error_message, retry_count
                FROM deliveries
                WHERE order_id = ?
                ORDER BY scheduled_at DESC
                LIMIT 8
                """,
                (order_id,),
            ).fetchall()
            click_rows = conn.execute(
                """
                SELECT delivery_id, COUNT(*) AS clicks
                FROM metric_snapshots
                WHERE delivery_id IN (SELECT id FROM deliveries WHERE order_id = ?)
                  AND metric_type = 'bot_start'
                GROUP BY delivery_id
                """,
                (order_id,),
            ).fetchall()
        clicks_by_delivery = {row["delivery_id"]: row["clicks"] for row in click_rows}
        tz_name = account.get("timezone") or DEFAULT_USER_TIMEZONE

        slot_label = self._slot_name(order["slot_type"]) if order["slot_type"] else "—"
        budget_cents = int(order["budget_cents"])
        spent_cents = int(order["spent_cents"])
        reserved_cents = int(order["reserved_cents"])
        lines = [
            f"📋 订单详情 {order['id']}",
            "",
            f"📺 频道：{order['channel_title']}",
            f"📊 状态：{self._status_label(order['status'])}",
            f"🧩 展示：{slot_label}",
            f"💵 预算：USD {cents_to_money(budget_cents)}"
            f"｜冻结 USD {cents_to_money(reserved_cents)}"
            f"｜已花 USD {cents_to_money(spent_cents)}",
            f"🕒 计划开始：{self._format_local_time(order['scheduled_at'], tz_name)}",
        ]
        if order["end_at"]:
            lines.append(f"🕒 计划结束：{self._format_local_time(order['end_at'], tz_name)}")
        lines.append("")
        if deliveries:
            total_clicks = sum(clicks_by_delivery.values())
            lines.append(f"🚀 发布记录（近 {len(deliveries)} 条，详情页点击合计 {total_clicks}）")
            for delivery in deliveries:
                status_icon = self._delivery_status_icon(delivery["status"])
                when_label = self._format_local_time(delivery["sent_at"] or delivery["scheduled_at"], tz_name)
                row_parts = [f"{status_icon} {when_label}"]
                if delivery["status"] in {"sent", "confirmed"} and delivery["charge_cents"]:
                    row_parts.append(f"扣费 USD {cents_to_money(int(delivery['charge_cents']))}")
                clicks = clicks_by_delivery.get(delivery["id"], 0)
                if clicks:
                    row_parts.append(f"点击 {clicks}")
                if delivery["error_message"] and delivery["status"] == "failed":
                    short_err = self._short_title(delivery["error_message"], 32)
                    row_parts.append(f"原因：{short_err}")
                if delivery["retry_count"] and delivery["status"] == "failed":
                    row_parts.append(f"重试 {delivery['retry_count']}")
                lines.append("• " + " · ".join(row_parts))
        else:
            lines.append("🚀 暂无发布记录")

        # Only 'sent' deliveries are disputable (matches DisputeService rule):
        # confirmed / refunded are post-settlement, disputed is already in flight.
        has_disputable_delivery = any(d["status"] == "sent" for d in deliveries)
        has_open_dispute = any(d["status"] == "disputed" for d in deliveries)
        keyboard: list[list[dict[str, str]]] = []
        action_row: list[dict[str, str]] = []
        if order["status"] in self.orders.ADVERTISER_PAUSABLE_STATUSES:
            action_row.append({"text": "⏸ 停止投放", "callback_data": f"advertiser:order:{order['id']}:stop"})
        if has_disputable_delivery and not has_open_dispute:
            action_row.append({"text": "🚩 申诉", "callback_data": f"advertiser:order:{order['id']}:dispute"})
        if action_row:
            keyboard.append(action_row)
        keyboard.append([{"text": "📋 投放订单", "callback_data": "advertiser:orders"}])
        keyboard.append([{"text": "💰 广告钱包", "callback_data": "advertiser:balance"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _send_advertiser_order_stop_confirm(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        order_id: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(
                conn, user_id, "advertiser", user.get("first_name") or user.get("username")
            )
            order = conn.execute(
                """
                SELECT o.id, o.status, o.reserved_cents, c.title AS channel_title
                FROM ad_orders o
                JOIN channels c ON c.id = o.channel_id
                WHERE o.id = ? AND o.advertiser_account_id = ?
                """,
                (order_id, account["id"]),
            ).fetchone()
        if not order:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 订单不存在或不属于你。",
                inline_keyboard=[
                    [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return
        if order["status"] not in self.orders.ADVERTISER_PAUSABLE_STATUSES:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"ℹ️ 订单当前状态「{self._status_label(order['status'])}」无法停止。",
                inline_keyboard=[
                    [{"text": "📋 订单详情", "callback_data": f"advertiser:order:{order_id}"}],
                    [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                ],
            )
            return
        text = (
            "⏸ 停止投放？\n\n"
            f"📺 频道：{order['channel_title']}\n"
            f"💵 将退回冻结预算 USD {cents_to_money(int(order['reserved_cents']))}\n\n"
            "停止后已发布的广告不会撤回，仅取消未发的剩余排期。"
        )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
            inline_keyboard=[
                [{"text": "✅ 确认停止", "callback_data": f"advertiser:order:{order_id}:stop_yes"}],
                [{"text": "↩️ 不停止，返回详情", "callback_data": f"advertiser:order:{order_id}"}],
            ],
        )

    def _advertiser_stop_order(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        order_id: str,
        source_message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_id = user.get("id") or chat_id
        try:
            self.orders.advertiser_pause_order(
                order_id=order_id,
                advertiser_telegram_user_id=user_id,
            )
        except NotFound:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 订单不存在或不属于你。",
                inline_keyboard=[
                    [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                    [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                ],
            )
            return {"handled": True, "type": "callback_advertiser_order_stop_not_found", "order_id": order_id}
        except InvalidState as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"ℹ️ 无法停止：{exc}",
                inline_keyboard=[
                    [{"text": "📋 订单详情", "callback_data": f"advertiser:order:{order_id}"}],
                    [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                ],
            )
            return {"handled": True, "type": "callback_advertiser_order_stop_invalid", "order_id": order_id}
        # 成功 — pause_and_release 已发了"投放已暂停"私信,这里直接刷回详情让用户立刻看到新状态
        self._send_advertiser_order_detail(chat_id, user, order_id, source_message)
        return {"handled": True, "type": "callback_advertiser_order_stopped", "order_id": order_id}

    def _begin_advertiser_dispute(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        order_id: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(
                conn, user_id, "advertiser", self._display_name(user)
            )
            order = conn.execute(
                "SELECT id FROM ad_orders WHERE id = ? AND advertiser_account_id = ?",
                (order_id, account["id"]),
            ).fetchone()
            if not order:
                self._reply_or_edit(
                    chat_id=chat_id,
                    source_message=source_message,
                    text="⚠️ 订单不存在或不属于你。",
                    inline_keyboard=[
                        [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                        [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
                    ],
                )
                return
            self._set_conversation_conn(
                conn,
                chat_id,
                account["id"],
                "dispute_open",
                "reason",
                {"order_id": order_id},
            )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "🚩 发起申诉\n\n"
                "请发送申诉原因（最多 500 字）。\n"
                "运营会暂停涉事投放、看证据后裁决退款。\n"
                "举例：频道主提前删除广告 / 修改素材 / 没有发布。"
            ),
            inline_keyboard=[
                [{"text": "↩️ 取消", "callback_data": f"advertiser:order:{order_id}"}],
            ],
        )

    def _handle_dispute_open_message(
        self,
        message: dict[str, Any],
        state: dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        if not chat_id:
            return {"handled": False, "reason": "missing_chat"}
        payload = json.loads(state["payload_json"] or "{}")
        order_id = payload.get("order_id")
        if state["step"] != "reason" or not order_id:
            self._clear_dispute_open_state(chat_id)
            return {"handled": True, "type": "dispute_invalid_state"}
        clean_text = (text or "").strip()
        if not clean_text:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="申诉原因不能为空，请重新发送。",
                inline_keyboard=[[{"text": "↩️ 取消", "callback_data": f"advertiser:order:{order_id}"}]],
            )
            return {"handled": True, "type": "dispute_empty"}
        user_id = user.get("id") or chat_id
        try:
            dispute = self.disputes.advertiser_open_dispute(
                advertiser_telegram_user_id=user_id,
                order_id=order_id,
                reason=clean_text,
            )
        except (NotFound, InvalidState) as exc:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text=f"⚠️ 申诉失败：{exc}",
                inline_keyboard=[[{"text": "📋 订单详情", "callback_data": f"advertiser:order:{order_id}"}]],
            )
            self._clear_dispute_open_state(chat_id)
            return {"handled": True, "type": "dispute_failed", "error": str(exc)}
        self._clear_dispute_open_state(chat_id)
        self.gateway.send_private_message(
            chat_id=chat_id,
            text=(
                f"🚩 申诉已提交（{dispute['id']}）\n\n"
                "运营会复核证据并裁决，结果会在订单详情页和私信通知。"
            ),
            inline_keyboard=[
                [{"text": "📋 订单详情", "callback_data": f"advertiser:order:{order_id}"}],
                [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ],
        )
        return {"handled": True, "type": "dispute_opened", "dispute_id": dispute["id"], "order_id": order_id}

    def _clear_dispute_open_state(self, chat_id: str | int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT flow FROM bot_conversation_states WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
            if row and row["flow"] == "dispute_open":
                conn.execute(
                    "DELETE FROM bot_conversation_states WHERE chat_id = ?",
                    (str(chat_id),),
                )

    def _delivery_status_icon(self, status: str) -> str:
        return {
            "scheduled": "🕒",
            "sent": "✅",
            "confirmed": "✅",
            "failed": "❌",
            "refunded": "↩️",
        }.get(status, "•")

    def _format_local_time(self, value: str | None, tz_name: str | None = None) -> str:
        if not value:
            return "—"
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                dt = dt.astimezone(ZoneInfo(tz_name))
            except Exception:
                pass
        return dt.strftime("%Y-%m-%d %H:%M")

    def _send_channel_quote(self, chat_id: str | int, channel_id: str, source_message: dict[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            channel = self._sync_channel_profile_conn(conn, self.channels.get_channel(conn, channel_id))
            rates = conn.execute(
                """
                SELECT s.slot_type, r.unit_price_cents, r.currency
                FROM ad_slots s
                JOIN rate_cards r ON r.slot_id = s.id AND r.active = 1
                WHERE s.channel_id = ? AND s.enabled = 1
                ORDER BY s.slot_type
                """,
                (channel_id,),
            ).fetchall()
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join([f"💵 {channel['title']}", *self._rate_lines(rates)]),
            inline_keyboard=[self._channel_keyboard(channel_id)[0], [{"text": "🏠 主菜单", "callback_data": "menu:home"}]],
        )

    def _send_channel_order_help(self, chat_id: str | int, channel_id: str, source_message: dict[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            channel = self._sync_channel_profile_conn(conn, self.channels.get_channel(conn, channel_id))
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=f"🧾 {channel['title']}\n\n选广告位，发文案和链接，再设预算。",
            inline_keyboard=[[{"text": "💵 价格", "callback_data": f"channel:quote:{channel_id}"}], [{"text": "🏠 主菜单", "callback_data": "menu:home"}]],
        )

    def _start_publisher_onboarding(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        display_name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part) or user.get("username")
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "publisher", display_name)
        self._set_conversation(chat_id, account["id"], "publisher_onboarding", "await_channel_forward", {})
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "🔌 接入频道\n\n"
                "1 把 Bot 加为频道管理员\n"
                "2 开启发消息、编辑消息\n"
                "3 从频道转发任意消息给我\n\n"
                "我会自动识别并检查权限。"
            ),
            inline_keyboard=self._cancel_keyboard("❌ 取消接入", "flow:cancel"),
        )

    def _handle_publisher_onboarding_message(self, message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        user_id = user.get("id") or chat_id
        channel_info = self._extract_forwarded_channel(message)
        if not channel_info:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text="⚠️ 没识别到频道\n\n请从频道转发任意消息给我。",
                inline_keyboard=self._cancel_keyboard("❌ 取消接入", "flow:cancel"),
            )
            return {"handled": True, "type": "publisher_onboarding_need_forward"}

        permission = self._check_channel_permissions(channel_info["id"], user_id)
        if not permission["ok"]:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text=(
                    "⚠️ 暂不能接入\n\n"
                    f"📺 {channel_info['title']}\n"
                    + "\n".join(self._permission_lines(permission))
                    + "\n\n请补齐权限后重试。"
                ),
                inline_keyboard=self._cancel_keyboard("❌ 取消接入", "flow:cancel"),
            )
            return {"handled": True, "type": "publisher_onboarding_permission_failed", "permissions": permission}

        display_name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part) or user.get("username")
        channel = self.channels.bind_channel(
            telegram_chat_id=channel_info["id"],
            title=channel_info["title"],
            username=channel_info.get("username"),
            owner_telegram_user_id=user_id,
            owner_display_name=display_name,
        )
        self._sync_channel_admins(channel["id"], channel["telegram_chat_id"])
        self._clear_conversation(chat_id)
        rates = self._channel_rates(channel["id"])
        self.gateway.send_private_message(
            chat_id=chat_id,
            text=(
                "✅ 频道已接入\n\n"
                f"📺 {channel['title']}\n"
                f"🔗 {self.channels.start_url(channel)}\n\n"
                "💵 默认价格：\n"
                + "\n".join(self._rate_lines(rates))
                + "\n\n"
                + "\n".join(self._permission_lines(permission))
            ),
            inline_keyboard=[
                [{"text": "⚙️ 广告形态", "callback_data": f"pub:formats:{channel['ref_token']}"}],
                [{"text": "💵 价格", "callback_data": f"channel:quote:{channel['id']}"}],
                [{"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}],
            ],
        )
        return {"handled": True, "type": "publisher_channel_bound", "channel_id": channel["id"]}

    def _extract_forwarded_channel(self, message: dict[str, Any]) -> dict[str, Any] | None:
        origin = message.get("forward_origin") or {}
        if origin.get("type") == "channel":
            chat = origin.get("chat") or {}
            if chat.get("id"):
                return {
                    "id": str(chat["id"]),
                    "title": chat.get("title") or chat.get("username") or "未命名频道",
                    "username": self._normalize_username(chat.get("username")),
                }

        forwarded_chat = message.get("forward_from_chat") or {}
        if forwarded_chat.get("type") == "channel" and forwarded_chat.get("id"):
            return {
                "id": str(forwarded_chat["id"]),
                "title": forwarded_chat.get("title") or forwarded_chat.get("username") or "未命名频道",
                "username": self._normalize_username(forwarded_chat.get("username")),
            }

        sender_chat = message.get("sender_chat") or {}
        if sender_chat.get("type") == "channel" and sender_chat.get("id"):
            return {
                "id": str(sender_chat["id"]),
                "title": sender_chat.get("title") or sender_chat.get("username") or "未命名频道",
                "username": self._normalize_username(sender_chat.get("username")),
            }
        return None

    def _normalize_username(self, username: str | None) -> str | None:
        return username.lstrip("@") if username else None

    def _display_name(self, user: dict[str, Any]) -> str | None:
        name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part)
        return name or user.get("username")

    def _ensure_mixed_account(self, user: dict[str, Any], fallback_user_id: str | int) -> dict[str, Any]:
        user_id = user.get("id") or fallback_user_id
        with self.db.transaction() as conn:
            return self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))

    def _prompt_timezone(
        self,
        chat_id: str | int,
        account_id: str,
        pending_start_payload: str,
        source_message: dict[str, Any] | None,
        *,
        conn: Any | None = None,
    ) -> None:
        payload = {"pending_start_payload": pending_start_payload}
        if conn is not None:
            self._set_conversation_conn(conn, chat_id, account_id, "timezone_setup", "confirm", payload)
        else:
            self._set_conversation(chat_id, account_id, "timezone_setup", "confirm", payload)
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "🌐 时区确认\n\n"
                f"默认使用北京时间：{self._timezone_now(DEFAULT_USER_TIMEZONE)}\n\n"
                "你当前使用这个时区吗？"
            ),
            inline_keyboard=[
                [{"text": "✅ 是的", "callback_data": "timezone:yes"}, {"text": "🌐 重选", "callback_data": "timezone:no"}],
            ],
        )

    def _confirm_timezone(self, chat_id: str | int, user: dict[str, Any], timezone_name: str) -> str:
        account = self._ensure_mixed_account(user, chat_id)
        state = self._get_conversation(chat_id)
        payload = json.loads(state["payload_json"] or "{}") if state else {}
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET timezone = ?, timezone_confirmed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (timezone_name, account["id"]),
            )
            conn.execute("DELETE FROM bot_conversation_states WHERE chat_id = ?", (str(chat_id),))
        return payload.get("pending_start_payload") or ""

    def _handle_timezone_message(self, message: dict[str, Any], state: dict[str, Any], text: str) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        clean_text = text.strip()
        timezone_name = self._resolve_timezone(clean_text)
        if not timezone_name:
            self.gateway.send_private_message(
                chat_id=chat_id,
                text=(
                    "⚠️ 没找到这个时区。\n\n"
                    "请发送城市或 IANA 时区名，例如：北京、Manila、Asia/Shanghai、Europe/Rome。"
                ),
                inline_keyboard=[[{"text": "使用北京时间", "callback_data": "timezone:yes"}]],
            )
            return {"handled": True, "type": "timezone_invalid"}
        pending_payload = self._confirm_timezone(chat_id, user, timezone_name)
        self.gateway.send_private_message(
            chat_id=chat_id,
            text=f"✅ 时区已设置\n\n当前：{self._timezone_now(timezone_name)}",
        )
        self._continue_after_timezone(chat_id, user, pending_payload, None)
        return {"handled": True, "type": "timezone_set", "timezone": timezone_name}

    def _continue_after_timezone(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        pending_start_payload: str,
        source_message: dict[str, Any] | None,
    ) -> None:
        if pending_start_payload:
            self._handle_start(
                {
                    "from": user,
                    "chat": {"id": chat_id},
                    "text": f"/start {pending_start_payload}",
                },
                pending_start_payload,
            )
            return
        self._send_main_menu(chat_id, source_message, user)

    def _resolve_timezone(self, raw_value: str) -> str | None:
        return resolve_timezone(raw_value)

    def _timezone_now(self, timezone_name: str) -> str:
        return format_timezone_now(timezone_name)

    def _add_channel_url(self) -> str:
        return f"https://t.me/{self.settings.bot_username}?startchannel&admin=post_messages+edit_messages+pin_messages"

    def _sync_channel_profile(self, channel: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            return self._sync_channel_profile_conn(conn, channel)

    def _sync_channel_profile_conn(self, conn: Any, channel: dict[str, Any]) -> dict[str, Any]:
        try:
            chat = self.gateway.get_chat(chat_id=channel["telegram_chat_id"])
        except TelegramError:
            return channel
        title = chat.get("title") or chat.get("username") or channel["title"]
        username = self._normalize_username(chat.get("username"))
        if title == channel.get("title") and username == channel.get("username"):
            return channel
        conn.execute(
            """
            UPDATE channels
            SET title = ?, username = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (title, username, channel["id"]),
        )
        return {**channel, "title": title, "username": username}

    def _publisher_channels_for_user(self, user_id: str | int, display_name: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "publisher", display_name)
            candidates = self.channels.list_admin_channels(conn, user_id)
            if not candidates:
                candidates = conn.execute(
                    """
                    SELECT c.*
                    FROM channels c
                    WHERE c.owner_account_id = ? AND c.status = 'active'
                    ORDER BY c.updated_at DESC, c.created_at DESC
                    """,
                    (account["id"],),
                ).fetchall()
                candidates = [dict(row) for row in candidates]

        visible: list[dict[str, Any]] = []
        for channel in candidates:
            try:
                member = self.gateway.get_chat_member(chat_id=channel["telegram_chat_id"], user_id=user_id)
                if member.get("status") in {"creator", "administrator"}:
                    with self.db.transaction() as conn:
                        self.channels.record_channel_admin(
                            conn,
                            channel_id=channel["id"],
                            telegram_user_id=user_id,
                            status=member.get("status", "administrator"),
                            display_name=display_name,
                            is_bot=False,
                            can_post_messages=bool(member.get("can_post_messages")),
                            can_edit_messages=bool(member.get("can_edit_messages")),
                            can_pin_messages=bool(member.get("can_pin_messages")),
                        )
                    visible.append(self._sync_channel_profile(channel))
            except TelegramError:
                # If Telegram cannot answer right now, keep previously bound ownership visible.
                with self.db.transaction() as conn:
                    owner = conn.execute("SELECT telegram_user_id FROM accounts WHERE id = ?", (channel["owner_account_id"],)).fetchone()
                if owner and str(owner["telegram_user_id"]) == str(user_id):
                    visible.append(self._sync_channel_profile(channel))
        return visible

    def _user_can_manage_channel(self, user_id: str | int, channel: dict[str, Any]) -> bool:
        try:
            member = self.gateway.get_chat_member(chat_id=channel["telegram_chat_id"], user_id=user_id)
            if member.get("status") in {"creator", "administrator"}:
                with self.db.transaction() as conn:
                    self.channels.record_channel_admin(
                        conn,
                        channel_id=channel["id"],
                        telegram_user_id=user_id,
                        status=member.get("status", "administrator"),
                        display_name=None,
                        is_bot=False,
                        can_post_messages=bool(member.get("can_post_messages")),
                        can_edit_messages=bool(member.get("can_edit_messages")),
                        can_pin_messages=bool(member.get("can_pin_messages")),
                    )
                return True
        except TelegramError:
            pass
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM channels c
                LEFT JOIN accounts owner ON owner.id = c.owner_account_id
                LEFT JOIN channel_admins ca
                  ON ca.channel_id = c.id
                 AND ca.telegram_user_id = ?
                 AND ca.status IN ('creator', 'administrator')
                WHERE c.id = ?
                  AND (owner.telegram_user_id = ? OR ca.telegram_user_id IS NOT NULL)
                LIMIT 1
                """,
                (str(user_id), channel["id"], str(user_id)),
            ).fetchone()
            return row is not None

    def _sync_channel_admins(self, channel_id: str, telegram_chat_id: str | int) -> list[dict[str, Any]]:
        try:
            members = self.gateway.get_chat_administrators(chat_id=telegram_chat_id)
        except TelegramError as exc:
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_logs (id, action, entity_type, entity_id, payload_json)
                    VALUES (?, 'sync_channel_admins_failed', 'channel', ?, ?)
                    """,
                    (new_id("aud"), channel_id, json.dumps({"error": str(exc)}, ensure_ascii=False)),
                )
                return self.channels.list_channel_admins(conn, channel_id)
        with self.db.transaction() as conn:
            return self.channels.sync_channel_admins(conn, channel_id, members)

    def _notify_channel_admins(self, channel: dict[str, Any], admins: list[dict[str, Any]], actor_id: str | int) -> int:
        notified = 0
        for admin in admins:
            if admin["is_bot"]:
                continue
            try:
                self.gateway.send_private_message(
                    chat_id=admin["telegram_user_id"],
                    text=(
                        "✅ 频道已加入插播\n\n"
                        f"📺 {channel['title']}\n"
                        "现在可以配置广告形态、价格和频道入口。"
                    ),
                    inline_keyboard=[
                        [{"text": "⚙️ 管理频道", "callback_data": f"pub:channel:{channel['ref_token']}"}],
                        [{"text": "📺 频道管理", "callback_data": "publisher:channels"}],
                    ],
                )
                notified += 1
            except TelegramError:
                continue
        if not any(str(admin["telegram_user_id"]) == str(actor_id) for admin in admins if not admin["is_bot"]):
            try:
                self.gateway.send_private_message(
                    chat_id=actor_id,
                    text=f"✅ {channel['title']} 已加入插播，可以开始配置。",
                    inline_keyboard=[[{"text": "⚙️ 管理频道", "callback_data": f"pub:channel:{channel['ref_token']}"}]],
                )
                notified += 1
            except TelegramError:
                pass
        return notified

    def _check_channel_permissions(self, channel_chat_id: str | int, publisher_user_id: str | int) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "publisher_status": "unknown",
            "bot_status": "unknown",
            "can_post_messages": False,
            "can_edit_messages": False,
            "can_pin_messages": False,
            "errors": [],
        }
        try:
            publisher_member = self.gateway.get_chat_member(chat_id=channel_chat_id, user_id=publisher_user_id)
            result["publisher_status"] = publisher_member.get("status", "unknown")
        except TelegramError as exc:
            result["errors"].append(f"publisher_check_failed: {exc}")

        try:
            bot = self.gateway.get_me()
            bot_member = self.gateway.get_chat_member(chat_id=channel_chat_id, user_id=bot["id"])
            result["bot_status"] = bot_member.get("status", "unknown")
            result["can_post_messages"] = bool(bot_member.get("can_post_messages"))
            result["can_edit_messages"] = bool(bot_member.get("can_edit_messages"))
            result["can_pin_messages"] = bool(bot_member.get("can_pin_messages"))
        except TelegramError as exc:
            result["errors"].append(f"bot_check_failed: {exc}")

        publisher_ok = result["publisher_status"] in {"creator", "administrator"}
        bot_ok = result["bot_status"] in {"creator", "administrator"} and result["can_post_messages"] and result["can_edit_messages"]
        result["ok"] = publisher_ok and bot_ok
        return result

    def _permission_lines(self, permission: dict[str, Any]) -> list[str]:
        publisher = "通过" if permission["publisher_status"] in {"creator", "administrator"} else f"未通过（{permission['publisher_status']}）"
        bot_admin = "通过" if permission["bot_status"] in {"creator", "administrator"} else f"未通过（{permission['bot_status']}）"
        post = "通过" if permission["can_post_messages"] else "未开启"
        edit = "通过" if permission["can_edit_messages"] else "未开启"
        pin = "通过" if permission["can_pin_messages"] else "未开启，置顶广告会不可用"
        lines = [
            f"频道主权限：{publisher}",
            f"Bot 管理员权限：{bot_admin}",
            f"Bot 发消息权限：{post}",
            f"Bot 编辑消息权限：{edit}",
            f"Bot 置顶权限：{pin}",
        ]
        if permission["errors"]:
            lines.append("权限检查错误：" + "；".join(permission["errors"]))
        return lines

    def _permission_summary(self, permission: dict[str, Any]) -> str:
        parts = [
            "主理人✅" if permission["publisher_status"] in {"creator", "administrator"} else "主理人⚠️",
            "发帖✅" if permission["can_post_messages"] else "发帖⚠️",
            "编辑✅" if permission["can_edit_messages"] else "编辑⚠️",
            "置顶✅" if permission["can_pin_messages"] else "置顶⚠️",
        ]
        return " ".join(parts)

    def _channel_admin_count(self, channel_id: str) -> int:
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM channel_admins
                WHERE channel_id = ?
                  AND status IN ('creator', 'administrator')
                  AND is_bot = 0
                """,
                (channel_id,),
            ).fetchone()
            return int(row["count"] if row else 0)

    def _send_publisher_formats(self, chat_id: str | int, user: dict[str, Any], channel_identifier: str, source_message: dict[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            channel = self._sync_channel_profile_conn(conn, self._find_channel(conn, channel_identifier))
            rows = conn.execute(
                """
                SELECT p.format_type, p.enabled, p.owner_price_band, p.platform_promo_enabled,
                       r.unit_price_cents, r.currency
                FROM channel_ad_format_policies p
                JOIN ad_slots s ON s.channel_id = p.channel_id AND s.slot_type = p.format_type
                JOIN rate_cards r ON r.slot_id = s.id AND r.active = 1
                WHERE p.channel_id = ?
                ORDER BY p.format_type
                """,
                (channel["id"],),
            ).fetchall()
        if not self._user_can_manage_channel(user.get("id") or chat_id, channel):
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 未确认你是该频道管理员。",
                inline_keyboard=[[{"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}]],
            )
            return
        lines = [f"⚙️ {channel['title']}", "", "点击开关广告形态。"]
        toggle_buttons = []
        for row in rows:
            status = "✅" if row["enabled"] else "⛔"
            promo = "带入口" if row["platform_promo_enabled"] else "无入口"
            label = self._slot_label(row["format_type"])
            lines.append(f"{status} {label}｜{row['currency']} {row['unit_price_cents'] / 100:.2f}｜{promo}")
            action = "⛔ 关" if row["enabled"] else "✅ 开"
            toggle_buttons.append({"text": f"{action} {self._slot_name(row['format_type'])}", "callback_data": f"pub:toggle:{channel['ref_token']}:{row['format_type']}"})
        keyboard = self._button_grid(toggle_buttons, 2)
        keyboard.append([{"text": "💵 价格", "callback_data": f"channel:quote:{channel['id']}"}])
        keyboard.append([{"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}])
        self._reply_or_edit(chat_id=chat_id, source_message=source_message, text="\n".join(lines), inline_keyboard=keyboard)

    def _toggle_publisher_format(self, chat_id: str | int, user: dict[str, Any], channel_identifier: str, slot_type: str, source_message: dict[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            channel = self._find_channel(conn, channel_identifier)
            policy = conn.execute(
                "SELECT * FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = ?",
                (channel["id"], self.channels.normalize_slot_type(slot_type)),
            ).fetchone()
            if not policy:
                raise NotFound(f"format policy not found: {slot_type}")
            enabled = not bool(policy["enabled"])
        if not self._user_can_manage_channel(user.get("id") or chat_id, channel):
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 未确认你是该频道管理员。",
                inline_keyboard=[[{"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}]],
            )
            return
        try:
            self.channels.set_format_policy(
                channel["id"],
                slot_type,
                enabled=enabled,
                owner_price_band=policy["owner_price_band"],
                platform_promo_enabled=bool(policy["platform_promo_enabled"]),
                custom_multiplier_bps=policy["custom_multiplier_bps"],
            )
        except ChaboError as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 无法修改：{exc}",
                inline_keyboard=[[{"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}]],
            )
            return
        self._send_publisher_formats(chat_id, user, channel["ref_token"], source_message)

    def _resolve_publisher_channel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            channel = self._find_channel(conn, channel_identifier)
        if not self._user_can_manage_channel(user.get("id") or chat_id, channel):
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 未确认你是该频道管理员。",
                inline_keyboard=[[{"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}]],
            )
            return None
        return channel

    def _send_publisher_approval_settings(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                f"⚙️ 接广告设置\n\n频道：{channel['title']}\n\n"
                "默认：规则内自动接单。\n\n"
                "需要人工确认的订单：\n"
                "· 定制插播\n"
                "· 高风险类目\n"
                "· 超出当日频控\n\n"
                "其余订单按当前展示形态、价格档位和频控自动接单。"
            ),
            inline_keyboard=[
                [{"text": "🧩 展示形态", "callback_data": f"pub:formats:{channel['ref_token']}"}, {"text": "💵 价格档位", "callback_data": f"pub:band:{channel['ref_token']}"}],
                [{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}],
            ],
        )

    def _send_publisher_band_picker(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return
        with self.db.transaction() as conn:
            policies = conn.execute(
                """
                SELECT format_type, owner_price_band, enabled
                FROM channel_ad_format_policies
                WHERE channel_id = ?
                  AND format_type IN ('light_tail', 'standard_card', 'strong_post')
                ORDER BY format_type
                """,
                (channel["id"],),
            ).fetchall()
        labels = {"low": "低档 0.85x", "medium": "中档 1.0x", "high": "高档 1.25x"}
        lines = [f"💵 价格档位\n\n频道：{channel['title']}", ""]
        keyboard: list[list[dict[str, str]]] = []
        for policy in policies:
            current = policy["owner_price_band"] if policy["owner_price_band"] in labels else "medium"
            status = "✅" if policy["enabled"] else "⛔"
            lines.append(f"{status} {self._slot_name(policy['format_type'])}：{labels.get(current, current)}")
            row: list[dict[str, str]] = []
            for band in ("low", "medium", "high"):
                marker = "✅ " if current == band else ""
                row.append(
                    {
                        "text": f"{marker}{labels[band].split(' ')[0]}",
                        "callback_data": f"pub:band:set:{channel['ref_token']}:{policy['format_type']}:{band}",
                    }
                )
            keyboard.append(row)
        keyboard.append([{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _set_publisher_band(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        format_type: str,
        band: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return
        if band not in {"low", "medium", "high"}:
            self._send_publisher_band_picker(chat_id, user, channel["ref_token"], source_message)
            return
        with self.db.transaction() as conn:
            policy = conn.execute(
                "SELECT * FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = ?",
                (channel["id"], self.channels.normalize_slot_type(format_type)),
            ).fetchone()
        if not policy:
            self._send_publisher_band_picker(chat_id, user, channel["ref_token"], source_message)
            return
        user_id = user.get("id") or chat_id
        try:
            self.channels.set_format_policy_for_publisher(
                publisher_telegram_user_id=user_id,
                channel_id=channel["id"],
                format_type=format_type,
                enabled=bool(policy["enabled"]),
                owner_price_band=band,
                platform_promo_enabled=bool(policy["platform_promo_enabled"]),
                custom_multiplier_bps=policy["custom_multiplier_bps"],
            )
        except ChaboError as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 无法修改：{exc}",
                inline_keyboard=[[{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}]],
            )
            return
        self._send_publisher_band_picker(chat_id, user, channel["ref_token"], source_message)

    def _send_publisher_limit_panel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return
        with self.db.transaction() as conn:
            config = conn.execute(
                "SELECT daily_ad_limit, allowed_start_hour, allowed_end_hour FROM channel_configs WHERE channel_id = ?",
                (channel["id"],),
            ).fetchone()
        limit = config["daily_ad_limit"] if config else 3
        start = config["allowed_start_hour"] if config else 9
        end = config["allowed_end_hour"] if config else 23
        text = (
            f"⏱ 频控时间\n\n频道：{channel['title']}\n\n"
            f"每日最多：{limit} 条\n"
            f"可投时间：{start:02d}:00 — {end:02d}:00\n\n"
            "调整每日上限："
        )
        adjust_row: list[dict[str, str]] = []
        for option in (1, 2, 3, 5, 10):
            marker = "✅ " if option == limit else ""
            adjust_row.append(
                {
                    "text": f"{marker}{option}",
                    "callback_data": f"pub:limit:set:{channel['ref_token']}:{option}",
                }
            )
        keyboard = [
            adjust_row,
            [{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}],
        ]
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
            inline_keyboard=keyboard,
        )

    def _set_publisher_daily_limit(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        limit: int,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return
        user_id = user.get("id") or chat_id
        try:
            self.channels.set_daily_ad_limit_for_publisher(
                publisher_telegram_user_id=user_id,
                channel_id=channel["id"],
                daily_ad_limit=limit,
            )
        except ChaboError as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 无法修改：{exc}",
                inline_keyboard=[[{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}]],
            )
            return
        self._send_publisher_limit_panel(chat_id, user, channel["ref_token"], source_message)

    def _send_publisher_self_promo_panel(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return
        user_id = user.get("id") or chat_id
        materials = self.self_promos.list_publishable_materials(
            publisher_telegram_user_id=user_id
        )
        lines = [
            f"🪧 自用发布\n\n频道：{channel['title']}",
            "",
            "给自己的频道发布运营内容或广告。",
            "自用发布不扣广告费，但会保留插播增长入口。",
        ]
        keyboard: list[list[dict[str, str]]] = []
        if materials:
            lines.append("")
            lines.append("选择一条素材发布：")
            for material in materials:
                preview = self._short_title((material["text"] or "").replace("\n", " "), 18)
                label = f"{self._slot_name(material['format_type'])}｜{preview}"
                keyboard.append(
                    [
                        {
                            "text": f"📤 {label}",
                            "callback_data": f"pub:self:pick:{channel['ref_token']}:{material['id']}",
                        }
                    ]
                )
        else:
            lines.append("")
            lines.append("还没有可用的标准/定制插播素材。")
            lines.append("先去【🎯 频道招商】或 CLI 创建一条素材，再回到这里发布。")
        keyboard.append([{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _publish_self_promo(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        material_id: str,
        source_message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return {"handled": True, "type": "callback_self_promo_unauthorized"}
        user_id = user.get("id") or chat_id
        try:
            prepared = self.self_promos.prepare_publish(
                publisher_telegram_user_id=user_id,
                channel_id=channel["id"],
                material_id=material_id,
            )
        except ChaboError as exc:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 自用发布失败\n\n{exc}",
                inline_keyboard=[[{"text": "⬅️ 自用发布", "callback_data": f"pub:self:{channel['ref_token']}"}]],
            )
            return {"handled": True, "type": "callback_self_promo_failed", "error": str(exc)}

        material = prepared["material"]
        self_promo_id = prepared["self_promo_id"]
        sales_url = f"https://t.me/{self.settings.bot_username}?start=ch_{channel['ref_token']}"
        track_url = f"https://t.me/{self.settings.bot_username}?start=sp_{self_promo_id}"
        cta_text = (material["button_text"] or "").strip() or "查看详情"
        keyboard = [
            [
                {"text": "📣 频道招商", "url": sales_url},
                {"text": "🔍 查看详情", "url": track_url},
            ],
            [{"text": cta_text, "url": material["target_url"]}],
        ]
        from .telegram import TelegramError as _TelegramError  # local import to avoid top-level cycle
        try:
            message_id = self.gateway.send_ad(
                chat_id=channel["telegram_chat_id"],
                text=material["text"],
                inline_keyboard=keyboard,
            )
        except _TelegramError as exc:
            self.self_promos.mark_failed(self_promo_id, error=str(exc))
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=f"⚠️ 自用发布失败\n\n{exc}",
                inline_keyboard=[[{"text": "⬅️ 自用发布", "callback_data": f"pub:self:{channel['ref_token']}"}]],
            )
            return {"handled": True, "type": "callback_self_promo_failed", "error": str(exc)}

        self.self_promos.mark_sent(self_promo_id, message_id=message_id)
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                f"✅ 自用发布成功\n\n频道：{channel['title']}\n"
                f"素材：{self._short_title((material['text'] or '').replace(chr(10), ' '), 24)}\n"
                f"消息：{message_id}\n\n"
                "已附带频道招商 + 查看详情 + 广告主 CTA 三按钮。"
            ),
            inline_keyboard=[
                [{"text": "🔁 再发一条", "callback_data": f"pub:self:{channel['ref_token']}"}],
                [{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}],
            ],
        )
        return {
            "handled": True,
            "type": "callback_self_promo_published",
            "self_promo_id": self_promo_id,
            "message_id": message_id,
        }

    def _send_publisher_channel_stats(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return
        with self.db.transaction() as conn:
            stats = self._channel_dashboard_stats(conn, channel["id"])
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS sent_total,
                    COALESCE(SUM(CASE WHEN sent_at >= datetime('now', '-7 days') THEN 1 ELSE 0 END), 0) AS sent_week,
                    COALESCE(SUM(charge_cents), 0) AS gross_total
                FROM deliveries
                WHERE channel_id = ? AND status IN ('sent', 'confirmed')
                """,
                (channel["id"],),
            ).fetchone()
        text = (
            f"📊 数据\n\n频道：{channel['title']}\n\n"
            f"今日已发：{stats['today_ads']} / {stats['daily_limit']}\n"
            f"近 7 天发布：{totals['sent_week']} 条\n"
            f"累计发布：{totals['sent_total']} 条\n"
            f"累计成交：USD {cents_to_money(totals['gross_total'])}\n"
            f"待确认收益：USD {cents_to_money(stats['pending_earnings_cents'])}"
        )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
            inline_keyboard=[[{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}]],
        )

    def _send_publisher_channel_earnings(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        channel_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel = self._resolve_publisher_channel(chat_id, user, channel_identifier, source_message)
        if not channel:
            return
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'sent' THEN publisher_net_cents - publisher_reversed_cents ELSE 0 END), 0) AS pending,
                    COALESCE(SUM(CASE WHEN status = 'confirmed' THEN publisher_net_cents - publisher_reversed_cents ELSE 0 END), 0) AS confirmed,
                    COALESCE(SUM(platform_fee_cents - platform_fee_reversed_cents), 0) AS platform_fee
                FROM deliveries
                WHERE channel_id = ?
                """,
                (channel["id"],),
            ).fetchone()
        text = (
            f"💸 收益明细\n\n频道：{channel['title']}\n\n"
            f"⏳ 待确认：USD {cents_to_money(row['pending'])}\n"
            f"✅ 已确认：USD {cents_to_money(row['confirmed'])}\n"
            f"📊 平台已收：USD {cents_to_money(row['platform_fee'])}"
        )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
            inline_keyboard=[
                [{"text": "💸 我的全部收益", "callback_data": "publisher:earnings"}],
                [{"text": "⬅️ 返回频道", "callback_data": f"pub:channel:{channel['ref_token']}"}],
            ],
        )

    def _start_order_flow(self, chat_id: str | int, user: dict[str, Any], channel_id: str, source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            self.accounts.get_or_create_by_telegram(conn, user_id, "advertiser", user.get("first_name") or user.get("username"))
            channel = self._sync_channel_profile_conn(conn, self.channels.get_channel(conn, channel_id))
            rates = conn.execute(
                """
                SELECT s.slot_type, r.unit_price_cents, r.currency
                FROM ad_slots s
                JOIN rate_cards r ON r.slot_id = s.id AND r.active = 1
                WHERE s.channel_id = ? AND s.enabled = 1
                ORDER BY s.slot_type
                """,
                (channel_id,),
            ).fetchall()
        self._clear_conversation(chat_id)
        self._send_channel_sales_landing(chat_id, channel, rates, source_message)

    def _set_order_slot(self, chat_id: str | int, user: dict[str, Any], channel_id: str, slot_type: str, source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "advertiser", user.get("first_name") or user.get("username"))
            channel = self._sync_channel_profile_conn(conn, self.channels.get_channel(conn, channel_id))
            creatives = conn.execute(
                """
                SELECT cr.*
                FROM creatives cr
                JOIN campaigns ca ON ca.id = cr.campaign_id
                WHERE ca.advertiser_account_id = ? AND cr.status != 'rejected'
                ORDER BY cr.updated_at DESC, cr.created_at DESC
                LIMIT 5
                """,
                (account["id"],),
            ).fetchall()
            creative_ids = [row["id"] for row in creatives]
        normalized_slot = self.channels.normalize_slot_type(slot_type)
        payload = {"channel_id": channel_id, "slot_type": normalized_slot, "creative_ids": creative_ids}
        if creatives:
            self._set_conversation(chat_id, account["id"], "create_order", "choose_creative", payload)
            lines = [
                "📄 选择广告素材",
                "",
                f"频道：{channel['title']}",
                f"位置：{self._slot_label(normalized_slot)}",
                "",
                "选择已有广告，或新建一个。",
            ]
            keyboard = []
            for index, creative in enumerate(creatives):
                label = self._short_title((creative["text"] or "").replace("\n", " "), 18)
                keyboard.append([{"text": f"📄 {label}", "callback_data": f"order:pick:{index}"}])
            keyboard.append([{"text": "➕ 新建广告", "callback_data": "order:newcreative"}])
            keyboard.append([{"text": "⬅️ 重选广告形式", "callback_data": f"channel:order:{channel_id}"}])
            self._reply_or_edit(chat_id=chat_id, source_message=source_message, text="\n".join(lines), inline_keyboard=keyboard)
            return

        self._begin_new_order_creative(chat_id, user, source_message, payload=payload, account_id=account["id"])

    def _begin_new_order_creative(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        account_id: str | None = None,
    ) -> None:
        if payload is None:
            state = self._get_conversation(chat_id)
            if not state:
                self._reply_or_edit(
                    chat_id=chat_id,
                    source_message=source_message,
                    text="⚠️ 当前投放步骤已失效，请从频道入口重新开始。",
                    inline_keyboard=[[{"text": "🏠 工作台", "callback_data": "menu:home"}]],
                )
                return
            payload = json.loads(state["payload_json"] or "{}")
            account_id = state["account_id"]
        normalized_slot = self.channels.normalize_slot_type(payload["slot_type"])
        first_step = "light_short_text" if normalized_slot == "light_tail" else "creative_text"
        self._set_conversation(chat_id, account_id, "create_order", first_step, payload)
        if normalized_slot == "light_tail":
            text = (
                "➕ 新建轻插播广告\n\n"
                "轻插播会在频道最新帖子底部放一行短入口，尽量不打扰阅读。\n"
                "用户点击后，会打开 Bot 里的完整广告详情。\n\n"
                "第一步：请发送 15 个字以内的短入口。\n"
                "例如：领资料、点我下单、限时福利"
            )
        else:
            text = (
                f"➕ 新建{self._slot_name(normalized_slot)}广告\n\n"
                "请发送广告文案。"
            )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
            inline_keyboard=self._cancel_keyboard(),
        )

    def _select_order_creative(self, chat_id: str | int, user: dict[str, Any], index: int, source_message: dict[str, Any] | None = None) -> None:
        state = self._get_conversation(chat_id)
        if not state or state["flow"] != "create_order" or state["step"] != "choose_creative":
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 当前投放步骤已失效，请从频道入口重新开始。",
                inline_keyboard=[[{"text": "🏠 工作台", "callback_data": "menu:home"}]],
            )
            return
        payload = json.loads(state["payload_json"] or "{}")
        creative_ids = payload.get("creative_ids") or []
        if index < 0 or index >= len(creative_ids):
            self._reply_or_edit(chat_id=chat_id, source_message=source_message, text="⚠️ 没有这个广告素材。", inline_keyboard=self._cancel_keyboard())
            return
        with self.db.transaction() as conn:
            creative = conn.execute("SELECT * FROM creatives WHERE id = ?", (creative_ids[index],)).fetchone()
        if not creative:
            self._reply_or_edit(chat_id=chat_id, source_message=source_message, text="⚠️ 广告素材不存在。", inline_keyboard=self._cancel_keyboard())
            return
        payload.update(
            {
                "creative_text": creative["text"],
                "target_url": creative["target_url"],
                "button_text": creative["button_text"],
                "selected_creative_id": creative["id"],
            }
        )
        self._set_conversation(chat_id, state["account_id"], "create_order", "budget", payload)
        self._ask_order_budget(chat_id, payload, source_message, prefix="✅ 已选广告素材")

    def _ask_order_budget(
        self,
        chat_id: str | int,
        payload: dict[str, Any],
        source_message: dict[str, Any] | None = None,
        *,
        prefix: str = "✅ 广告已准备好",
    ) -> None:
        price = self._slot_price_cents(payload["channel_id"], payload["slot_type"])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                f"{prefix}\n\n"
                "第三步：设置本次预算。\n"
                f"最低 USD {cents_to_money(price)}，例如：20"
            ),
            inline_keyboard=self._cancel_keyboard(),
        )

    def _get_conversation(self, chat_id: str | int) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM bot_conversation_states WHERE chat_id = ?", (str(chat_id),)).fetchone()
            return dict(row) if row else None

    def _set_conversation(self, chat_id: str | int, account_id: str | None, flow: str, step: str, payload: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            self._set_conversation_conn(conn, chat_id, account_id, flow, step, payload)

    def _set_conversation_conn(self, conn: Any, chat_id: str | int, account_id: str | None, flow: str, step: str, payload: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO bot_conversation_states (chat_id, account_id, flow, step, payload_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                account_id = excluded.account_id,
                flow = excluded.flow,
                step = excluded.step,
                payload_json = excluded.payload_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(chat_id), account_id, flow, step, json.dumps(payload, ensure_ascii=False)),
        )

    def _clear_conversation(self, chat_id: str | int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM bot_conversation_states WHERE chat_id = ?", (str(chat_id),))

    def _slot_price_cents(self, channel_id: str, slot_type: str) -> int:
        with self.db.transaction() as conn:
            rate = self.channels.get_rate(conn, channel_id, slot_type)
            return int(rate["unit_price_cents"])

    def _placement_slot_prices(self, channel_id: str) -> dict[str, int]:
        """Per-slot unit_price_cents for the placement display panel.

        Single query to label all three format buttons with their per-delivery
        price; the cost panel still computes the full quote (period × pin × ...).
        """
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT s.slot_type, r.unit_price_cents
                FROM ad_slots s
                JOIN rate_cards r ON r.slot_id = s.id AND r.active = 1
                WHERE s.channel_id = ?
                """,
                (channel_id,),
            ).fetchall()
        return {row["slot_type"]: int(row["unit_price_cents"]) for row in rows}

    def _find_channel(self, conn: Any, identifier: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM channels WHERE id = ? OR ref_token = ? OR telegram_chat_id = ? OR username = ?",
            (identifier, identifier, identifier, self._normalize_username(identifier)),
        ).fetchone()
        if not row:
            raise NotFound(f"channel not found: {identifier}")
        return dict(row)

    def _channel_rates(self, channel_id: str) -> list[Any]:
        with self.db.transaction() as conn:
            return conn.execute(
                """
                SELECT s.slot_type, r.unit_price_cents, r.currency
                FROM ad_slots s
                JOIN rate_cards r ON r.slot_id = s.id AND r.active = 1
                WHERE s.channel_id = ? AND s.enabled = 1
                ORDER BY s.slot_type
                """,
                (channel_id,),
            ).fetchall()

    def _cancel_keyboard(self, text: str = "取消创建", callback_data: str = "order:cancel") -> list[list[dict[str, str]]]:
        return [[{"text": text, "callback_data": callback_data}]]

    def _channel_keyboard(self, channel_id: str) -> list[list[dict[str, str]]]:
        return [
            [{"text": "🧾 创建订单", "callback_data": f"channel:order:{channel_id}"}],
            [{"text": "💵 价格", "callback_data": f"channel:quote:{channel_id}"}, {"text": "💰 广告钱包", "callback_data": "advertiser:balance"}],
        ]

    def _slot_picker_keyboard(self, channel_id: str, rates: list[Any]) -> list[list[dict[str, str]]]:
        slot_buttons = [
            {"text": self._slot_label(rate["slot_type"]), "callback_data": f"order:slot:{channel_id}:{rate['slot_type']}"}
            for rate in rates
        ]
        keyboard = self._button_grid(slot_buttons, 2)
        keyboard.append([{"text": "💵 价格说明", "callback_data": f"channel:quote:{channel_id}"}, {"text": "🏠 工作台", "callback_data": "menu:home"}])
        return keyboard

    def _channel_tokens_from_start_payload(self, payload: str) -> list[str]:
        if not payload:
            return []
        if payload.startswith("ch_"):
            return [payload.removeprefix("ch_"), payload]
        return [payload]

    def _rate_lines(self, rates: list[Any]) -> list[str]:
        return [f"• {self._slot_label(rate['slot_type'])}：{rate['currency']} {rate['unit_price_cents'] / 100:.2f}" for rate in rates]

    def _slot_name(self, slot_type: str) -> str:
        normalized = self.channels.normalize_slot_type(slot_type)
        return SLOT_DISPLAY_NAMES.get(normalized, slot_type)

    def _slot_label(self, slot_type: str) -> str:
        normalized = self.channels.normalize_slot_type(slot_type)
        emoji = SLOT_EMOJIS.get(normalized, "📍")
        return f"{emoji} {SLOT_DISPLAY_NAMES.get(normalized, slot_type)}"

    def _short_title(self, title: str, limit: int) -> str:
        return title if len(title) <= limit else title[:limit] + "..."

    def _build_ad_detail_view(
        self,
        creative: sqlite3.Row,
        source_channel: sqlite3.Row,
    ) -> tuple[str, list[list[dict[str, str]]]]:
        title = source_channel["title"] or "未命名频道"
        username = source_channel["username"]
        channel_label = f"@{username}" if username else title
        text = "\n".join(
            [
                "📄 插播广告详情",
                "",
                creative["text"],
                "",
                f"📺 来源频道：{title}（{channel_label}）",
            ]
        )
        cta_text = (creative["button_text"] or "").strip() or "查看链接"
        keyboard = [
            [{"text": cta_text, "url": creative["target_url"]}],
            [{"text": "📣 我也想在这个频道投广告", "callback_data": f"channel:order:{source_channel['id']}"}],
            [{"text": "🏠 工作台", "callback_data": "menu:home"}],
        ]
        return text, keyboard

    def _creative_status_label(self, status: str) -> str:
        labels = {
            "pending_review": "⏳ 待审",
            "approved": "✅ 已审",
            "rejected": "⛔ 拒绝",
        }
        return labels.get(status, status)

    def _button_grid(self, buttons: list[dict[str, str]], width: int) -> list[list[dict[str, str]]]:
        return [buttons[index : index + width] for index in range(0, len(buttons), width)]

    def _reply_or_edit(
        self,
        *,
        chat_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        source_message: dict[str, Any] | None = None,
    ) -> str | None:
        message_id = source_message.get("message_id") if source_message else None
        if message_id:
            try:
                self.gateway.edit_private_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    inline_keyboard=inline_keyboard,
                )
                return str(message_id)
            except TelegramError:
                pass
        return self.gateway.send_private_message(chat_id=chat_id, text=text, inline_keyboard=inline_keyboard)

    def _status_label(self, status: str) -> str:
        labels = {
            "pending_review": "⏳ 待审",
            "approved": "✅ 已审",
            "running": "🚀 投放中",
            "budget_exhausted": "💸 已用完",
            "refunded": "↩️ 已退",
            "rejected": "⛔ 拒绝",
            "paused": "⏸ 暂停",
            "done": "✅ 完成",
        }
        return labels.get(status, status)

    def _merge_channel_buttons(
        self,
        post: dict[str, Any],
        promo_url: str,
        probe: dict[str, Any] | None = None,
    ) -> list[list[dict[str, str]]]:
        reply_markup = post.get("reply_markup") or {}
        existing = reply_markup.get("inline_keyboard") or []
        keyboard: list[list[dict[str, str]]] = []
        for row in existing:
            keyboard.append([{"text": button.get("text", ""), "url": button.get("url", "")} for button in row if button.get("url")])
        if probe:
            probe_url = self.light_probes.start_url(probe)
            probe_button = {"text": probe["button_text"], "url": probe_url}
            if not any(button.get("url") == probe_url for row in keyboard for button in row):
                keyboard.append([probe_button])
        promo_button = {"text": "📣 频道招商", "url": promo_url}
        if not any(button.get("url") == promo_url for row in keyboard for button in row):
            keyboard.append([promo_button])
        return keyboard

    def _handle_successful_payment(self, message: dict[str, Any]) -> dict[str, Any]:
        payment = message["successful_payment"]
        payload = payment.get("invoice_payload", "")
        if payload.startswith("stars:"):
            user = message.get("from") or {}
            return self.stars_payments.fulfill_successful_payment(payment, telegram_user_id=user.get("id"))
        if not payload.startswith("topup:"):
            return {"handled": False, "reason": "unsupported_payment_payload"}
        account_id = payload.removeprefix("topup:")
        if payment.get("currency") != "XTR":
            return {"handled": False, "reason": "non_stars_payment_rejected"}
        # Telegram Stars have no public fiat face value here. The invoice payload should
        # map Stars to internal credits before sending the invoice; total_amount is stored as cents.
        amount_cents = int(payment.get("total_amount", 0))
        charge_id = payment.get("telegram_payment_charge_id", "")
        with self.db.transaction() as conn:
            self.ledger.stars_topup(conn, account_id, amount_cents, charge_id)
        return {"handled": True, "type": "stars_topup", "account_id": account_id, "amount_cents": amount_cents}
