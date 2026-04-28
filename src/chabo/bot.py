from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .db import Database
from .ids import new_id
from .money import cents_to_money, money_to_cents
from .services import AccountService, ChaboError, ChannelService, LedgerService, LightProbeService, NotFound, OrderService, StarsPaymentService
from .telegram import MessageGateway, TelegramError


DEFAULT_USER_TIMEZONE = "Asia/Shanghai"


TIMEZONE_ALIASES = {
    "beijing": "Asia/Shanghai",
    "北京": "Asia/Shanghai",
    "北京时间": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "中国": "Asia/Shanghai",
    "manila": "Asia/Manila",
    "马尼拉": "Asia/Manila",
    "philippines": "Asia/Manila",
    "菲律宾": "Asia/Manila",
    "hongkong": "Asia/Hong_Kong",
    "hong kong": "Asia/Hong_Kong",
    "香港": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei",
    "台北": "Asia/Taipei",
    "taiwan": "Asia/Taipei",
    "台湾": "Asia/Taipei",
    "singapore": "Asia/Singapore",
    "新加坡": "Asia/Singapore",
    "tokyo": "Asia/Tokyo",
    "东京": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "首尔": "Asia/Seoul",
    "bangkok": "Asia/Bangkok",
    "曼谷": "Asia/Bangkok",
    "dubai": "Asia/Dubai",
    "迪拜": "Asia/Dubai",
    "rome": "Europe/Rome",
    "罗马": "Europe/Rome",
    "london": "Europe/London",
    "伦敦": "Europe/London",
    "new york": "America/New_York",
    "newyork": "America/New_York",
    "纽约": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "losangeles": "America/Los_Angeles",
    "洛杉矶": "America/Los_Angeles",
}


SLOT_DISPLAY_NAMES = {
    "light_tail": "轻插播",
    "standard": "标准插播",
    "standard_card": "标准插播",
    "strong_post": "强插播",
    "pin24h": "置顶 24h",
    "loop_daily": "循环插播",
}

SLOT_EMOJIS = {
    "light_tail": "🔖",
    "standard": "🖼",
    "standard_card": "🖼",
    "strong_post": "🔥",
    "pin24h": "📌",
    "loop_daily": "🔁",
}


class UpdateHandler:
    def __init__(self, db: Database, settings: Settings, gateway: MessageGateway):
        self.db = db
        self.settings = settings
        self.gateway = gateway
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings)
        self.ledger = LedgerService(db, settings)
        self.light_probes = LightProbeService(db, settings)
        self.orders = OrderService(db, settings)
        self.stars_payments = StarsPaymentService(db, settings)

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
                    if creative:
                        self.gateway.send_private_message(
                            chat_id=chat.get("id", user_id),
                            text="\n".join(["📄 插播广告详情", "", creative["text"], "", f"🔗 {creative['target_url']}"]),
                            inline_keyboard=[[{"text": "🔗 打开链接", "url": creative["target_url"]}]],
                        )
                    else:
                        self.gateway.send_private_message(chat_id=chat.get("id", user_id), text="广告详情暂不可用")
                    return {"handled": True, "type": "ad_start", "delivery_id": delivery_id}
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
            session_id = new_id("sess")
            conn.execute(
                """
                INSERT INTO advertiser_sessions (id, advertiser_account_id, ref_channel_id, start_payload)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, account["id"], channel["id"] if channel else None, payload or "organic"),
            )
            if channel:
                rates = conn.execute(
                    """
                    SELECT s.slot_type, r.unit_price_cents, r.currency
                    FROM ad_slots s
                    JOIN rate_cards r ON r.slot_id = s.id AND r.active = 1
                    WHERE s.channel_id = ? AND s.enabled = 1
                    ORDER BY s.slot_type
                    """,
                    (channel["id"],),
                ).fetchall()
                self._send_channel_sales_landing(chat.get("id", user_id), channel, rates)
                return {"handled": True, "type": "channel_start", "channel_id": channel["id"], "session_id": session_id}
            organic_session_id = session_id
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
        if data == "advertiser:balance":
            self._send_advertiser_balance(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_balance"}
        if data == "advertiser:library":
            self._send_advertiser_library(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_library"}
        if data == "advertiser:orders":
            self._send_advertiser_orders(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_orders"}
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
        if data.startswith("channel:quote:"):
            channel_id = data.removeprefix("channel:quote:")
            self._send_channel_quote(chat_id, channel_id, message)
            return {"handled": True, "type": "callback_channel_quote", "channel_id": channel_id}
        if data.startswith("channel:order:"):
            channel_id = data.removeprefix("channel:order:")
            self._start_order_flow(chat_id, user, channel_id, message)
            return {"handled": True, "type": "callback_order_flow_started", "channel_id": channel_id}
        if data.startswith("order:slot:"):
            rest = data.removeprefix("order:slot:")
            channel_id, slot_type = rest.rsplit(":", 1)
            self._set_order_slot(chat_id, user, channel_id, slot_type, message)
            return {"handled": True, "type": "callback_order_slot_selected", "channel_id": channel_id, "slot_type": slot_type}
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
                [{"text": "🧾 创建广告", "callback_data": "advertiser:order_help"}],
                [{"text": "🗂 广告库", "callback_data": "advertiser:library"}, {"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                [{"text": "💰 广告钱包", "callback_data": "advertiser:balance"}, {"text": "💵 定价规则", "callback_data": "publisher:pricing"}],
                [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ],
        )

    def _send_channel_sales_landing(
        self,
        chat_id: str | int,
        channel: dict[str, Any],
        rates: list[Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        channel_title = channel["title"]
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(
                [
                    "📣 频道招商",
                    f"📺 {channel_title}",
                    "",
                    f"投广告到 {channel_title}",
                    "",
                    "💵 当前价",
                    *self._rate_lines(rates),
                    "",
                    "✅ 发布成功才扣费",
                ]
            ),
            inline_keyboard=self._channel_sales_keyboard(channel),
        )

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
                text = (creative["text"] or "").replace("\n", " ")
                if len(text) > 28:
                    text = text[:28] + "..."
                lines.append(f"{index}. {text}")
                lines.append(f"   {self._creative_status_label(creative['status'])} · {creative['button_text']}")
        else:
            lines.extend(
                [
                    "还没有可复用广告。",
                    "先从频道里的“频道招商”进入，创建第一条插播。",
                ]
            )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[
                [{"text": "🧾 创建广告", "callback_data": "advertiser:order_help"}],
                [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}, {"text": "💰 广告钱包", "callback_data": "advertiser:balance"}],
                [{"text": "💵 定价规则", "callback_data": "publisher:pricing"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ],
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
            channel = self._find_channel(conn, channel_identifier)
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
        rates = self._channel_rates(channel["id"])
        admin_count = self._channel_admin_count(channel["id"])
        ready = "✅ 可接广告" if permission["ok"] else "⚠️ 待补权限"
        lines = [
            f"📺 {channel['title']}",
            "",
            f"状态：{ready}",
            f"权限：{self._permission_summary(permission)}",
            f"管理员：{admin_count} 位",
            "",
            "💵 当前价",
            *self._rate_lines(rates),
            "",
            f"🔗 {self.channels.start_url(channel)}",
        ]
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[
                [{"text": "⚙️ 广告形态", "callback_data": f"pub:formats:{channel['ref_token']}"}],
                [{"text": "💵 价格", "callback_data": f"channel:quote:{channel['id']}"}, {"text": "🔄 检查权限", "callback_data": f"pub:refresh:{channel['ref_token']}"}],
                [{"text": "💸 收益", "callback_data": "publisher:earnings"}, {"text": "⬅️ 频道管理", "callback_data": "publisher:channels"}],
            ],
        )

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

    def _send_advertiser_balance(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", user.get("first_name") or user.get("username"))
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "💰 广告钱包\n\n"
                f"💵 可用：USD {account['available_balance_cents'] / 100:.2f}\n"
                f"🔒 冻结：USD {account['reserved_balance_cents'] / 100:.2f}\n"
                f"📊 已花：USD {account['spent_balance_cents'] / 100:.2f}"
            ),
            inline_keyboard=[[{"text": "📣 我的广告", "callback_data": "role:advertiser"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}]],
        )

    def _send_publisher_earnings(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "💸 我的收益\n\n"
                f"⏳ 待确认：USD {account['pending_earnings_cents'] / 100:.2f}\n"
                f"✅ 已确认：USD {account['confirmed_earnings_cents'] / 100:.2f}\n"
                f"💵 可结算：USD {account['releasable_earnings_cents'] / 100:.2f}"
            ),
            inline_keyboard=[[{"text": "📺 频道管理", "callback_data": "publisher:channels"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}]],
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
            price = self._slot_price_cents(payload["channel_id"], payload["slot_type"])
            self.gateway.send_private_message(
                chat_id=chat_id,
                text=f"请设置本次频道插播预算，最低 USD {cents_to_money(price)}。例如：20",
                inline_keyboard=self._cancel_keyboard(),
            )
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
        if rows:
            lines = ["📋 最近订单"]
            for row in rows:
                lines.append(f"• {row['title']}｜{self._status_label(row['status'])}｜预算 {row['budget_cents'] / 100:.2f}｜已花 {row['spent_cents'] / 100:.2f}")
        else:
            lines = ["📋 暂无订单\n\n从频道按钮进入即可创建。"]
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[[{"text": "📣 我的广告", "callback_data": "role:advertiser"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}]],
        )

    def _send_channel_quote(self, chat_id: str | int, channel_id: str, source_message: dict[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            channel = self.channels.get_channel(conn, channel_id)
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
            channel = self.channels.get_channel(conn, channel_id)
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
        value = raw_value.strip()
        if not value:
            return None
        normalized = value.lower().replace("_", " ")
        aliased = TIMEZONE_ALIASES.get(normalized) or TIMEZONE_ALIASES.get(normalized.replace(" ", ""))
        timezone_name = aliased or value
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return None
        return timezone_name

    def _timezone_now(self, timezone_name: str) -> str:
        now = datetime.now(ZoneInfo(timezone_name))
        return f"{timezone_name}（{now.strftime('%Y-%m-%d %H:%M')}）"

    def _add_channel_url(self) -> str:
        return f"https://t.me/{self.settings.bot_username}?startchannel&admin=post_messages+edit_messages+pin_messages"

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
                    visible.append(channel)
            except TelegramError:
                # If Telegram cannot answer right now, keep previously bound ownership visible.
                with self.db.transaction() as conn:
                    owner = conn.execute("SELECT telegram_user_id FROM accounts WHERE id = ?", (channel["owner_account_id"],)).fetchone()
                if owner and str(owner["telegram_user_id"]) == str(user_id):
                    visible.append(channel)
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
            channel = self._find_channel(conn, channel_identifier)
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

    def _start_order_flow(self, chat_id: str | int, user: dict[str, Any], channel_id: str, source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "advertiser", user.get("first_name") or user.get("username"))
            channel = self.channels.get_channel(conn, channel_id)
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
        keyboard = []
        for rate in rates:
            keyboard.append(
                [
                    {
                        "text": f"{self._slot_label(rate['slot_type'])} · {rate['currency']} {rate['unit_price_cents'] / 100:.2f}",
                        "callback_data": f"order:slot:{channel_id}:{rate['slot_type']}",
                    }
                ]
            )
        keyboard.append([{"text": "❌ 取消", "callback_data": "order:cancel"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=f"🧾 {channel['title']}\n请选择广告位：",
            inline_keyboard=keyboard,
        )

    def _set_order_slot(self, chat_id: str | int, user: dict[str, Any], channel_id: str, slot_type: str, source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "advertiser", user.get("first_name") or user.get("username"))
            channel = self.channels.get_channel(conn, channel_id)
            rate = self.channels.get_rate(conn, channel_id, slot_type)
        normalized_slot = self.channels.normalize_slot_type(slot_type)
        first_step = "light_short_text" if normalized_slot == "light_tail" else "creative_text"
        self._set_conversation(
            chat_id,
            account["id"],
            "create_order",
            first_step,
            {"channel_id": channel_id, "slot_type": normalized_slot},
        )
        if normalized_slot == "light_tail":
            text = (
                f"✅ 已选 {self._slot_label(slot_type)}\n"
                f"💵 USD {cents_to_money(rate['unit_price_cents'])}\n\n"
                "轻插播会在频道最新帖子底部放一行短入口，尽量不打扰阅读。\n"
                "用户点击后，会打开 Bot 里的完整广告详情。\n\n"
                "第一步：请发送 15 个字以内的短入口。\n"
                "例如：领资料、点我下单、限时福利"
            )
        else:
            text = (
                f"✅ 已选 {self._slot_label(slot_type)}\n"
                f"💵 USD {cents_to_money(rate['unit_price_cents'])}\n\n"
                "请直接发送广告文案。"
            )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
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

    def _channel_sales_keyboard(self, channel: dict[str, Any]) -> list[list[dict[str, str]]]:
        title = self._short_title(channel["title"], 12)
        return [
            [{"text": f"🧾 投广告到{title}", "callback_data": f"channel:order:{channel['id']}"}],
            [{"text": "🗂 广告库", "callback_data": "advertiser:library"}, {"text": "💰 广告钱包", "callback_data": "advertiser:balance"}],
            [{"text": "💵 价格说明", "callback_data": f"channel:quote:{channel['id']}"}, {"text": "🏠 工作台", "callback_data": "menu:home"}],
        ]

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
