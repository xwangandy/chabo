from __future__ import annotations

import html
import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import insert_audit_log
from .config import Settings
from .db import Database
from .ids import new_id
from .money import cents_to_money, money_to_cents
from .services import AccountService, AdvertiserService, ChaboError, ChannelService, InsufficientBalance, InvalidState, LedgerService, LightProbeService, MaterialService, NotFound, OrderService, SelfPromoService, StarsPaymentService
from .telegram import MessageGateway, TelegramError
from .timezones import DEFAULT_USER_TIMEZONE, TIMEZONE_ALIASES, format_timezone_now, resolve_timezone
from .webapi.auth import build_magic_link_url, issue_login_token_conn, sync_portal_access_from_activity


STALE_CHANNEL_POST_SECONDS = 10 * 60


SLOT_DISPLAY_NAMES = {
    "light_tail": "文字插播",
    "button_tail": "按钮插播",
    "standard": "标准插播",
    "standard_card": "标准插播",
    "strong_post": "定制插播",
    "pin24h": "置顶 24h",
    "loop_daily": "循环发布",
}

SLOT_EMOJIS = {
    "light_tail": "✍️",
    "button_tail": "🔘",
    "standard": "🧾",
    "standard_card": "🧾",
    "strong_post": "🎨",
    "pin24h": "📌",
    "loop_daily": "🔁",
}

PLACEMENT_SLOT_TYPES = ("strong_post", "standard_card", "button_tail", "light_tail")
PINNABLE_PLACEMENT_SLOTS = {"standard_card", "strong_post"}
SCHEDULED_PLACEMENT_SLOTS = {"standard_card", "strong_post"}
CHANNEL_PACED_PLACEMENT_SLOTS = {"button_tail", "light_tail"}
PLACEMENT_ASSET_STEPS = ("ad_name", "media_detail", "detail_text", "target_url", "button_text", "short_text", "standard_text")
PLACEMENT_ASSET_STEP_FIELDS = {
    "ad_name": ("creative_name",),
    "media_detail": ("media_file_id", "media_type"),
    "detail_text": ("creative_text",),
    "target_url": ("target_url",),
    "button_text": ("button_text",),
    "short_text": ("light_short_text",),
    "standard_text": ("standard_text",),
}
PLACEMENT_PERIODS = {
    "once": {"label": "仅发布一次", "deliveries": 1, "discount_bps": 10000},
    "week": {"label": "连续 1 周", "deliveries": 7, "discount_bps": 9000},
    "month": {"label": "连续 1 个月", "deliveries": 30, "discount_bps": 8000},
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
        text = message.get("text") or message.get("caption") or ""
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
            if not account["timezone_confirmed_at"]:
                self._prompt_timezone(chat.get("id", user_id), account["id"], payload, None, conn=conn)
                return {"handled": True, "type": "timezone_prompt", "pending_start_payload": payload}
            if payload in {"role", "switch", "settings"}:
                self._prompt_role(chat.get("id", user_id), account["id"], "", None, conn=conn, switch=True)
                return {"handled": True, "type": "role_prompt"}
            active_role = self._account_active_role(account)
            channel = None
            for channel_token in self._channel_tokens_from_start_payload(payload):
                channel = self.channels.get_by_token(conn, channel_token)
                if channel:
                    break
            if channel:
                channel = self._sync_channel_profile_conn(conn, channel)
                skip_advertiser_session = False
            elif not active_role:
                self._prompt_role(chat.get("id", user_id), account["id"], payload, None, conn=conn)
                return {"handled": True, "type": "role_prompt", "pending_start_payload": payload}
            elif active_role == "publisher":
                publisher_start_result = {"handled": True, "type": "organic_start", "active_role": active_role}
                if payload:
                    publisher_start_result["ignored_payload"] = payload
                channel_for_landing = None
                channel_start_result = None
                organic_session_id = None
                skip_advertiser_session = True
            else:
                skip_advertiser_session = False
            if not skip_advertiser_session:
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
        if "publisher_start_result" in locals():
            self._send_main_menu(chat.get("id", user_id), user=user)
            return publisher_start_result
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
            self.gateway.answer_callback_query(callback_query_id=query["id"])
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
            self._set_active_role(chat_id, user, "advertiser", message)
            return {"handled": True, "type": "callback_advertiser_menu"}
        if data == "role:publisher":
            self._set_active_role(chat_id, user, "publisher", message)
            return {"handled": True, "type": "callback_publisher_menu"}
        if data == "publisher:channels":
            self._send_publisher_menu(chat_id, user, message)
            return {"handled": True, "type": "callback_publisher_channels"}
        if data == "publisher:earnings":
            self._send_publisher_earnings(chat_id, user, message)
            return {"handled": True, "type": "callback_publisher_earnings"}
        if data == "publisher:disable_income_notifications":
            self._set_publisher_income_notifications(chat_id, user, message, enabled=False)
            return {"handled": True, "type": "callback_publisher_income_notifications_disabled"}
        if data == "publisher:enable_income_notifications":
            self._set_publisher_income_notifications(chat_id, user, message, enabled=True)
            return {"handled": True, "type": "callback_publisher_income_notifications_enabled"}
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
        if data == "advertiser:orders":
            self._send_advertiser_orders(chat_id, user, message)
            return {"handled": True, "type": "callback_advertiser_orders"}
        if data == "settings:home":
            self._send_settings_menu(chat_id, user, message)
            return {"handled": True, "type": "callback_settings_menu"}
        if data == "settings:role":
            account = self._ensure_mixed_account(user, chat_id)
            self._prompt_role(chat_id, account["id"], "", message, switch=True)
            return {"handled": True, "type": "callback_role_switch_prompt"}
        if data == "web:open":
            return self._send_web_magic_link(chat_id, user, message)
        if data.startswith("advertiser:order:"):
            order_id = data.removeprefix("advertiser:order:")
            self._send_advertiser_order_detail(chat_id, user, order_id, message)
            return {"handled": True, "type": "callback_advertiser_order_detail", "order_id": order_id}
        if data == "advertiser:order_help":
            self._send_global_placement(chat_id, user, message, payload=self._default_global_placement_payload(), panel="creative")
            return {"handled": True, "type": "callback_global_placement_started"}
        if data.startswith("market:"):
            return self._handle_channel_market_callback(data, chat_id, user, message)
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
        if data.startswith("pub:template:"):
            channel_identifier = data.removeprefix("pub:template:")
            self._send_channel_template_picker(chat_id, user, channel_identifier, message)
            return {"handled": True, "type": "callback_publisher_template_picker", "channel": channel_identifier}
        if data.startswith("pub:tplapply:"):
            rest = data.removeprefix("pub:tplapply:")
            target_identifier, source_identifier = rest.split(":", 1)
            self._apply_channel_template(chat_id, user, target_identifier, source_identifier, message)
            return {
                "handled": True,
                "type": "callback_publisher_template_applied",
                "target": target_identifier,
                "source": source_identifier,
            }
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
            self._send_channel_quote(chat_id, channel_id, user, message)
            return {"handled": True, "type": "callback_channel_quote", "channel_id": channel_id}
        if data.startswith("channel:order:"):
            channel_id = data.removeprefix("channel:order:")
            self._start_order_flow(chat_id, user, channel_id, message)
            return {"handled": True, "type": "callback_order_flow_started", "channel_id": channel_id}
        if data.startswith("launch:"):
            return self._handle_global_placement_callback(data, chat_id, user, message)
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
        post_date = int(post.get("date") or 0)
        if post_date:
            age_seconds = datetime.now(timezone.utc).timestamp() - post_date
            if age_seconds > STALE_CHANNEL_POST_SECONDS:
                return {"handled": True, "type": "stale_channel_post_skipped", "message_id": message_id}
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
                insert_audit_log(
                    conn,
                    actor_account_id=None,
                    action="append_button_failed",
                    entity_type="channel",
                    entity_id=channel["id"],
                    payload={"error": str(exc), "message_id": message_id},
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
        user = user or {}
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            active_role = self._account_active_role(account)
            if not active_role:
                self._prompt_role(chat_id, account["id"], "", source_message, conn=conn)
                return
        if active_role == "publisher":
            self._send_publisher_home(chat_id, user, source_message)
            return
        self._send_advertiser_menu(chat_id, user, source_message)

    def _send_publisher_home(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        channels = self._publisher_channels_for_user(user_id, self._display_name(user))
        channel_line = f"已接入频道：{len(channels)} 个" if channels else "还没有接入频道"
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "<b>📺 频道主工作台</b>\n\n"
                "<blockquote>接频道，设规则，看收益。\n"
                "这里专注频道变现，不展示广告主投放工具。</blockquote>\n\n"
                f"{self._h(channel_line)}"
            ),
            inline_keyboard=[
                [{"text": "➕ 添加频道", "url": self._add_channel_url()}],
                [{"text": "📺 频道管理", "callback_data": "publisher:channels"}],
                [{"text": "💸 我的收益", "callback_data": "publisher:earnings"}, {"text": "💵 定价规则", "callback_data": "publisher:pricing"}],
                [{"text": "🌐 打开网页端", "callback_data": "web:open"}],
                [{"text": "⚙️ 设置", "callback_data": "settings:home"}],
            ],
            parse_mode="HTML",
        )

    def _send_settings_menu(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
        timezone_name = account["timezone"] or DEFAULT_USER_TIMEZONE
        active_role = self._account_active_role(account)
        role_label = {"publisher": "频道主", "advertiser": "广告主"}.get(active_role or "", "未选择")
        income_notifications_enabled = bool(account["publisher_income_notifications_enabled"])
        notification_label = "已开启" if income_notifications_enabled else "已关闭"
        notification_button = (
            {"text": "🔕 关闭收入通知", "callback_data": "publisher:disable_income_notifications"}
            if income_notifications_enabled
            else {"text": "🔔 开启收入通知", "callback_data": "publisher:enable_income_notifications"}
        )
        keyboard = [
            [{"text": "🌐 修改时区", "callback_data": "timezone:change"}],
            [{"text": "🔁 切换身份", "callback_data": "settings:role"}],
            [{"text": "🌐 打开网页端", "callback_data": "web:open"}],
        ]
        keyboard.append([notification_button])
        keyboard.append([{"text": "🏠 返回工作台", "callback_data": "menu:home"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "⚙️ 设置\n\n"
                f"当前时区：{timezone_name}\n"
                f"当前身份：{role_label}\n"
                f"收入通知：{notification_label}\n"
                + "如果要换到另一套工作台，请在这里手动切换身份。"
            ),
            inline_keyboard=keyboard,
        )

    def _set_publisher_income_notifications(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None,
        *,
        enabled: bool,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            conn.execute(
                """
                UPDATE accounts
                SET publisher_income_notifications_enabled = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (1 if enabled else 0, account["id"]),
            )
        if enabled:
            text = "🔔 收入通知已开启\n\n以后频道广告成功发布时，会继续提醒你收益到账。"
            button = {"text": "🔕 关闭通知", "callback_data": "publisher:disable_income_notifications"}
        else:
            text = "🔕 收入通知已关闭\n\n以后广告成功发布时，不再推送这类到账提醒。可以在设置里重新开启。"
            button = {"text": "🔔 重新开启", "callback_data": "publisher:enable_income_notifications"}
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=text,
            inline_keyboard=[
                [button, {"text": "💰 我的钱包", "callback_data": "publisher:earnings"}],
            ],
        )

    def _send_channel_market_home(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None = None) -> None:
        state = self._get_conversation(chat_id)
        payload = json.loads(state["payload_json"] or "{}") if state and state["flow"] == "channel_market" else {}
        payload["offset"] = 0
        self._send_channel_market_browse(chat_id, user, source_message, payload=payload, offset=0)

    def _send_channel_market_browse(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        offset: int | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            if payload is None:
                state = conn.execute("SELECT * FROM bot_conversation_states WHERE chat_id = ?", (str(chat_id),)).fetchone()
                payload = json.loads(state["payload_json"] or "{}") if state and state["flow"] == "channel_market" else {}
            collection = self._channel_collection_by_id_conn(conn, payload.get("collection_id") or "")
            if not collection or collection["advertiser_account_id"] != account["id"]:
                collection = self._ensure_channel_collection_conn(conn, account["id"], "默认收藏夹")
            total = self._market_channel_count_conn(conn)
            current_offset = max(0, int(payload.get("offset") or 0) if offset is None else offset)
            if total and current_offset > total:
                current_offset = total
            channel = None if total and current_offset >= total else self._market_channel_at_conn(conn, current_offset)
            payload = {"offset": current_offset, "collection_id": collection["id"], "collection_name": collection["name"]}
            if channel:
                payload["channel_id"] = channel["id"]
            saved_count = self._channel_collection_count_conn(conn, collection["id"])
            is_saved = bool(channel and self._channel_in_collection_conn(conn, collection["id"], channel["id"]))
            prices = self._market_channel_prices_conn(conn, channel["id"]) if channel else []
            self._set_conversation_conn(conn, chat_id, account["id"], "channel_market", "browse", payload)
        if total and current_offset >= total:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=(
                    "<b>🔎 频道广场</b>\n\n"
                    + self._html_quote("已经看到最后一个频道了。\n如果你想投放的频道还没接入，可以把机器人推荐给频道主，让对方先把频道接进来。")
                ),
                inline_keyboard=self._market_end_keyboard(total),
                parse_mode="HTML",
            )
            return
        if not channel:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="<b>🔎 频道广场</b>\n\n" + self._html_quote("当前还没有可浏览的频道。"),
                inline_keyboard=[[{"text": "🏠 主菜单", "callback_data": "menu:home"}]],
                parse_mode="HTML",
            )
            return
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=self._market_channel_text(channel, prices, current_offset + 1, total, collection["name"], saved_count, is_saved),
            inline_keyboard=self._market_browse_keyboard(current_offset, total, collection["name"], is_saved),
            parse_mode="HTML",
        )

    def _send_channel_collections(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            state = conn.execute("SELECT * FROM bot_conversation_states WHERE chat_id = ?", (str(chat_id),)).fetchone()
            previous_payload = json.loads(state["payload_json"] or "{}") if state and state["flow"] == "channel_market" else {}
            self._ensure_channel_collection_conn(conn, account["id"], "默认收藏夹")
            collections = self._channel_collections_conn(conn, account["id"])
            payload = {
                "collection_ids": [row["id"] for row in collections],
                "offset": int(previous_payload.get("offset") or 0),
            }
            if previous_payload.get("channel_id"):
                payload["channel_id"] = previous_payload["channel_id"]
            if previous_payload.get("collection_id"):
                payload["collection_id"] = previous_payload["collection_id"]
                payload["collection_name"] = previous_payload.get("collection_name")
            self._set_conversation_conn(conn, chat_id, account["id"], "channel_market", "folders", payload)
        lines = [
            "<b>⭐ 频道收藏夹</b>",
            "",
            self._html_quote("选择一个收藏夹后，会直接回到频道广场继续刷。\n之后点击收藏，都会保存到当前收藏夹。"),
            "",
        ]
        if collections:
            for index, collection in enumerate(collections, start=1):
                lines.append(f"{index}. <b>{self._h(collection['name'])}｜{collection['channel_count']} 个频道</b>")
        else:
            lines.append("还没有收藏夹。")
        buttons = [
            {"text": f"{index + 1} {self._short_title(collection['name'], 12)}", "callback_data": f"market:folder:{index}"}
            for index, collection in enumerate(collections)
        ]
        keyboard = self._button_grid(buttons, 2)
        keyboard.append([{"text": "➕ 新建收藏夹", "callback_data": "market:new_folder"}])
        keyboard.append([{"text": "↩️ 返回广场", "callback_data": "market:browse"}])
        self._reply_or_edit(chat_id=chat_id, source_message=source_message, text="\n".join(lines), inline_keyboard=keyboard, parse_mode="HTML")

    def _send_channel_collection_detail(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None,
        *,
        collection_id: str,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            collection = self._channel_collection_by_id_conn(conn, collection_id)
            if not collection or collection["advertiser_account_id"] != account["id"]:
                self._send_channel_collections(chat_id, user, source_message)
                return
            channels = self._channel_collection_channels_conn(conn, collection_id, limit=10)
            payload = {"collection_ids": [collection_id], "collection_id": collection_id, "collection_name": collection["name"]}
            self._set_conversation_conn(conn, chat_id, account["id"], "channel_market", "folder_detail", payload)
        lines = [f"<b>⭐ {self._h(collection['name'])}</b>", "", f"已收藏 <code>{len(channels)} 个频道</code>。", ""]
        if channels:
            for index, channel in enumerate(channels, start=1):
                subscribers = self._compact_count(int(channel["subscribers"] or 0))
                link = self._channel_public_url(channel) or "暂无公开链接"
                lines.append(f"{index}. 👥 {self._h(subscribers)}｜<b>{self._h(self._short_title(channel['title'], 16))}</b>")
                lines.append(f"   {self._h(link)}")
        else:
            lines.append(self._html_quote("这个收藏夹还是空的。先去频道广场收藏频道。"))
        keyboard = []
        if channels:
            keyboard.append([{"text": "➕ 用这个收藏夹投放", "callback_data": "market:launch_folder:0"}])
        keyboard.append([{"text": "🔎 继续刷频道", "callback_data": "market:browse"}, {"text": "⭐ 收藏夹列表", "callback_data": "market:folders"}])
        keyboard.append([{"text": "🏠 主菜单", "callback_data": "menu:home"}])
        self._reply_or_edit(chat_id=chat_id, source_message=source_message, text="\n".join(lines), inline_keyboard=keyboard, parse_mode="HTML")

    def _market_channel_text(
        self,
        channel: Any,
        prices: list[Any],
        index: int,
        total: int,
        collection_name: str,
        saved_count: int,
        is_saved: bool,
    ) -> str:
        subscribers = self._compact_count(int(channel["subscribers"] or 0))
        views = self._compact_count(int(channel["median_24h_views"] or 0))
        score = channel["score"] if channel["score"] is not None else "待评估"
        risk = channel["risk_level"] or "待评估"
        category = channel["category"] or "未分类"
        link = self._channel_public_url(channel) or "暂无公开链接"
        price_lines = [f"{self._slot_label(row['format_type'])}  USD {cents_to_money(int(row['unit_price_cents'] or 0))}" for row in prices]
        if not price_lines:
            price_lines = ["暂无可投广告位"]
        link_line = f'链接：<a href="{self._h(link)}">{self._h(link)}</a>' if link.startswith("http") else f"链接：{self._h(link)}"
        saved_line = "已在当前收藏夹 ✅" if is_saved else "未收藏"
        metric_block = self._html_pre(
            [
                f"订阅量   {subscribers}",
                f"24h浏览  {views}",
                f"类目     {category}",
                f"评分     {score}",
                f"风险     {risk}",
            ]
        )
        return "\n".join(
            [
                f"<b>🔎 频道广场</b>\n<code>第 {index}/{total} 个频道</code>",
                "",
                f"<b>📺 {self._h(channel['title'])}</b>",
                metric_block,
                link_line,
                "",
                "<b>可投广告位</b>",
                self._html_quote("\n".join(price_lines)),
                "",
                f"<b>当前收藏夹：{self._h(collection_name)}（{saved_count} 个）</b>",
                f"状态：{self._h(saved_line)}",
                "",
                self._html_quote("喜欢就收藏；不合适就点下一个继续看。"),
            ]
        )

    def _market_browse_keyboard(self, offset: int, total: int, collection_name: str, is_saved: bool) -> list[list[dict[str, str]]]:
        keyboard = [
            [
                {"text": f"📁 {self._short_title(collection_name, 12)}", "callback_data": "market:folders"},
                {"text": "✅ 已收藏" if is_saved else "⭐ 收藏", "callback_data": "market:save"},
            ]
        ]
        nav_row: list[dict[str, str]] = []
        if offset > 0:
            nav_row.append({"text": "⬅️ 上一个", "callback_data": "market:prev"})
        if offset + 1 < total:
            nav_row.append({"text": "下一个 ➡️", "callback_data": "market:next"})
        elif total:
            nav_row.append({"text": "下一个 ➡️", "callback_data": "market:next"})
        if nav_row:
            keyboard.append(nav_row)
        return keyboard

    def _market_end_keyboard(self, total: int) -> list[list[dict[str, str]]]:
        keyboard: list[list[dict[str, str]]] = []
        if total:
            keyboard.append([{"text": "⬅️ 上一个", "callback_data": "market:prev"}])
        keyboard.append([{"text": "📣 推荐给频道主", "url": self._add_channel_url()}])
        keyboard.append([{"text": "🏠 返回主页", "callback_data": "menu:home"}])
        return keyboard

    def _ensure_channel_collection_conn(self, conn: Any, account_id: str, name: str) -> Any:
        clean_name = self._clean_collection_name(name)
        row = conn.execute(
            "SELECT * FROM advertiser_channel_collections WHERE advertiser_account_id = ? AND name = ?",
            (account_id, clean_name),
        ).fetchone()
        if row:
            return row
        collection_id = new_id("col")
        conn.execute(
            """
            INSERT INTO advertiser_channel_collections (id, advertiser_account_id, name)
            VALUES (?, ?, ?)
            """,
            (collection_id, account_id, clean_name),
        )
        return conn.execute("SELECT * FROM advertiser_channel_collections WHERE id = ?", (collection_id,)).fetchone()

    def _clean_collection_name(self, name: str) -> str:
        clean_name = " ".join((name or "").strip().split())
        if not clean_name:
            clean_name = "默认收藏夹"
        return clean_name[:24]

    def _channel_collection_by_id_conn(self, conn: Any, collection_id: str) -> Any | None:
        if not collection_id:
            return None
        return conn.execute("SELECT * FROM advertiser_channel_collections WHERE id = ?", (collection_id,)).fetchone()

    def _channel_collections_conn(self, conn: Any, account_id: str) -> list[Any]:
        return conn.execute(
            """
            SELECT c.*, COUNT(i.channel_id) AS channel_count
            FROM advertiser_channel_collections c
            LEFT JOIN advertiser_channel_collection_items i ON i.collection_id = c.id
            WHERE c.advertiser_account_id = ?
            GROUP BY c.id
            ORDER BY c.updated_at DESC, c.created_at DESC
            """,
            (account_id,),
        ).fetchall()

    def _channel_collection_count_conn(self, conn: Any, collection_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM advertiser_channel_collection_items WHERE collection_id = ?",
            (collection_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def _channel_in_collection_conn(self, conn: Any, collection_id: str, channel_id: str) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM advertiser_channel_collection_items WHERE collection_id = ? AND channel_id = ?",
                (collection_id, channel_id),
            ).fetchone()
        )

    def _save_channel_to_collection_conn(self, conn: Any, account_id: str, collection_id: str, channel_id: str) -> None:
        item_id = new_id("coli")
        conn.execute(
            """
            INSERT OR IGNORE INTO advertiser_channel_collection_items (id, collection_id, channel_id)
            VALUES (?, ?, ?)
            """,
            (item_id, collection_id, channel_id),
        )
        conn.execute(
            "UPDATE advertiser_channel_collections SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (collection_id,),
        )
        saved_id = new_id("save")
        conn.execute(
            """
            INSERT INTO advertiser_saved_channels (id, advertiser_account_id, channel_id, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(advertiser_account_id, channel_id)
            DO UPDATE SET note = excluded.note
            """,
            (saved_id, account_id, channel_id, collection_id),
        )

    def _channel_collection_channels_conn(self, conn: Any, collection_id: str, *, limit: int = 50) -> list[Any]:
        return conn.execute(
            """
            SELECT c.*,
                   COALESCE(stats.subscribers, 0) AS subscribers,
                   COALESCE(stats.median_24h_views, 0) AS median_24h_views
            FROM advertiser_channel_collection_items i
            JOIN channels c ON c.id = i.channel_id
            LEFT JOIN (
                SELECT a.*
                FROM channel_pricing_assessments a
                JOIN (
                    SELECT channel_id, MAX(created_at) AS created_at
                    FROM channel_pricing_assessments
                    GROUP BY channel_id
                ) latest ON latest.channel_id = a.channel_id AND latest.created_at = a.created_at
            ) stats ON stats.channel_id = c.id
            WHERE i.collection_id = ? AND c.status = 'active'
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (collection_id, limit),
        ).fetchall()

    def _market_channel_count_conn(self, conn: Any) -> int:
        row = conn.execute("SELECT COUNT(*) AS count FROM channels WHERE status = 'active'").fetchone()
        return int(row["count"] if row else 0)

    def _market_channel_at_conn(self, conn: Any, offset: int) -> Any | None:
        return conn.execute(
            """
            SELECT c.*,
                   stats.category,
                   stats.score,
                   stats.risk_level,
                   COALESCE(stats.subscribers, 0) AS subscribers,
                   COALESCE(stats.median_24h_views, 0) AS median_24h_views
            FROM channels c
            LEFT JOIN (
                SELECT a.*
                FROM channel_pricing_assessments a
                JOIN (
                    SELECT channel_id, MAX(created_at) AS created_at
                    FROM channel_pricing_assessments
                    GROUP BY channel_id
                ) latest ON latest.channel_id = a.channel_id AND latest.created_at = a.created_at
            ) stats ON stats.channel_id = c.id
            WHERE c.status = 'active'
            ORDER BY COALESCE(stats.subscribers, 0) DESC, c.updated_at DESC
            LIMIT 1 OFFSET ?
            """,
            (offset,),
        ).fetchone()

    def _market_channel_prices_conn(self, conn: Any, channel_id: str) -> list[Any]:
        return conn.execute(
            """
            SELECT p.format_type, r.unit_price_cents
            FROM channel_ad_format_policies p
            JOIN ad_slots s
              ON s.channel_id = p.channel_id
             AND s.slot_type = p.format_type
             AND s.enabled = 1
            JOIN rate_cards r
              ON r.slot_id = s.id
             AND r.active = 1
            WHERE p.channel_id = ? AND p.enabled = 1
            ORDER BY CASE p.format_type
                WHEN 'button_tail' THEN 1
                WHEN 'light_tail' THEN 2
                WHEN 'standard_card' THEN 3
                WHEN 'strong_post' THEN 4
                ELSE 9
            END
            """,
            (channel_id,),
        ).fetchall()

    def _collection_supported_channel_ids_conn(self, conn: Any, collection_id: str, slot_type: str) -> list[str]:
        rows = conn.execute(
            """
            SELECT c.id
            FROM advertiser_channel_collection_items i
            JOIN channels c ON c.id = i.channel_id
            JOIN channel_ad_format_policies p
              ON p.channel_id = c.id
             AND p.format_type = ?
             AND p.enabled = 1
            JOIN ad_slots s
              ON s.channel_id = c.id
             AND s.slot_type = p.format_type
             AND s.enabled = 1
            JOIN rate_cards r
              ON r.slot_id = s.id
             AND r.active = 1
            WHERE i.collection_id = ? AND c.status = 'active'
            ORDER BY i.created_at DESC
            """,
            (slot_type, collection_id),
        ).fetchall()
        return [str(row["id"]) for row in rows]

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
                "📣 广告主工作台\n\n"
                f"💰 可用：USD {balance:.2f}\n"
                f"🔒 冻结：USD {reserved:.2f}\n\n"
                "这里专注素材、频道挑选和投放预算。"
            ),
            inline_keyboard=[
                [{"text": "➕ 广告投放", "callback_data": "advertiser:order_help"}],
                [{"text": "🗂 广告库", "callback_data": "advertiser:library"}, {"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                [{"text": "🔎 频道广场", "callback_data": "market:home"}, {"text": "⭐ 频道收藏夹", "callback_data": "market:folders"}],
                [{"text": "🌐 打开网页端", "callback_data": "web:open"}],
                [{"text": "💰 广告钱包", "callback_data": "advertiser:balance"}, {"text": "⚙️ 设置", "callback_data": "settings:home"}],
            ],
        )

    def _send_channel_sales_landing(
        self,
        chat_id: str | int,
        channel: dict[str, Any],
        rates: list[Any],
        source_message: dict[str, Any] | None = None,
    ) -> None:
        del rates
        payload = self._default_placement_payload(channel["id"])
        self._send_placement_configurator(chat_id, {}, source_message, payload=payload, panel="creative")

    def _default_placement_payload(self, channel_id: str) -> dict[str, Any]:
        return {
            "channel_id": channel_id,
            "flow_version": "guided_v2",
            "slot_type": "",
            "pin": False,
            "period": "once",
            "scheduled_label": "立即发布",
            "creative_text": "",
            "target_url": "",
            "button_text": "打开链接",
            "creative_ids": [],
        }

    def _default_global_placement_payload(self) -> dict[str, Any]:
        payload = self._default_placement_payload("")
        payload["flow_version"] = "global_v1"
        payload["launch_mode"] = "global"
        payload["channel_ids"] = []
        return payload

    def _send_global_placement(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        panel: str = "creative",
    ) -> None:
        user_id = user.get("id") or chat_id
        display_name = self._display_name(user) or None
        if panel not in {"creative", "display", "channel"}:
            panel = "creative"
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", display_name)
            if payload is None:
                state = conn.execute("SELECT * FROM bot_conversation_states WHERE chat_id = ?", (str(chat_id),)).fetchone()
                if state and state["flow"] in {"global_placement", "placement_config"}:
                    payload = json.loads(state["payload_json"] or "{}")
                else:
                    payload = self._default_global_placement_payload()
            payload["launch_mode"] = "global"
            creatives: list[Any] = []
            channels: list[Any] = []
            collections: list[dict[str, Any]] = []
            if panel == "creative":
                payload["channel_offset"] = 0
                creatives = conn.execute(
                    """
                    SELECT cr.*, ca.name AS campaign_name
                    FROM creatives cr
                    JOIN campaigns ca ON ca.id = cr.campaign_id
                    WHERE ca.advertiser_account_id = ?
                      AND cr.status != 'rejected'
                      AND cr.archived_at IS NULL
                    ORDER BY cr.updated_at DESC, cr.created_at DESC
                    LIMIT 8
                    """,
                    (account["id"],),
                ).fetchall()
                payload["creative_ids"] = [row["id"] for row in creatives]
            elif panel == "channel":
                mode = payload.get("channel_pick_mode") or "folders"
                if mode == "channels":
                    slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
                    total = self._global_placement_channel_count(conn, slot_type)
                    offset = max(0, int(payload.get("channel_offset") or 0))
                    if total and offset >= total:
                        offset = max(0, ((total - 1) // 10) * 10)
                    payload["channel_offset"] = offset
                    payload["channel_total"] = total
                    channels = self._global_placement_channels(conn, slot_type, offset=offset)
                    payload["channel_ids"] = [row["id"] for row in channels]
                else:
                    payload["channel_pick_mode"] = "folders"
                    collections = self._global_collection_options_conn(conn, account["id"], self.channels.normalize_slot_type(payload.get("slot_type") or ""))
                    payload["collection_ids"] = [row["id"] for row in collections]
                    if not payload.get("selected_collection_ids"):
                        first_ready = next((row for row in collections if int(row["supported_count"]) > 0), None)
                        if first_ready:
                            self._apply_global_collection_selection_conn(conn, payload, [str(first_ready["id"])])
                    elif payload.get("selected_collection_ids"):
                        self._apply_global_collection_selection_conn(conn, payload, [str(item) for item in payload.get("selected_collection_ids") or []])
            self._set_conversation_conn(conn, chat_id, account["id"], "global_placement", panel, payload)
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=self._global_placement_text(payload, panel, channels, collections),
            inline_keyboard=self._global_placement_keyboard(payload, panel, creatives, channels, collections),
            parse_mode="HTML",
        )

    def _global_placement_channel_count(self, conn: Any, slot_type: str) -> int:
        if not slot_type:
            return 0
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM channels c
            JOIN channel_ad_format_policies p
              ON p.channel_id = c.id
             AND p.format_type = ?
             AND p.enabled = 1
            JOIN ad_slots s
              ON s.channel_id = c.id
             AND s.slot_type = p.format_type
             AND s.enabled = 1
            WHERE c.status = 'active'
            """,
            (slot_type,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def _global_placement_channels(self, conn: Any, slot_type: str, *, offset: int = 0, limit: int = 10) -> list[Any]:
        if not slot_type:
            return []
        return conn.execute(
            """
            SELECT c.*,
                   COALESCE(stats.subscribers, 0) AS subscribers,
                   r.unit_price_cents,
                   r.currency
            FROM channels c
            JOIN channel_ad_format_policies p
              ON p.channel_id = c.id
             AND p.format_type = ?
             AND p.enabled = 1
            JOIN ad_slots s
              ON s.channel_id = c.id
             AND s.slot_type = p.format_type
             AND s.enabled = 1
            JOIN rate_cards r
              ON r.slot_id = s.id
             AND r.active = 1
            LEFT JOIN (
                SELECT channel_id, MAX(subscribers) AS subscribers
                FROM channel_pricing_assessments
                GROUP BY channel_id
            ) stats ON stats.channel_id = c.id
            WHERE c.status = 'active'
            ORDER BY COALESCE(stats.subscribers, 0) DESC, c.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (slot_type, limit, offset),
        ).fetchall()

    def _global_collection_options_conn(self, conn: Any, account_id: str, slot_type: str) -> list[dict[str, Any]]:
        self._ensure_channel_collection_conn(conn, account_id, "默认收藏夹")
        collections = self._channel_collections_conn(conn, account_id)
        options: list[dict[str, Any]] = []
        for collection in collections:
            supported_ids = self._collection_supported_channel_ids_conn(conn, collection["id"], slot_type) if slot_type else []
            options.append(
                {
                    "id": str(collection["id"]),
                    "name": str(collection["name"]),
                    "channel_count": int(collection["channel_count"] or 0),
                    "supported_count": len(supported_ids),
                }
            )
        return options

    def _apply_global_collection_selection_conn(self, conn: Any, payload: dict[str, Any], collection_ids: list[str]) -> None:
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        selected_ids = list(dict.fromkeys(str(collection_id) for collection_id in collection_ids if collection_id))
        selected_channel_ids: list[str] = []
        selected_names: list[str] = []
        for collection_id in selected_ids:
            collection = self._channel_collection_by_id_conn(conn, collection_id)
            if not collection:
                continue
            supported_ids = self._collection_supported_channel_ids_conn(conn, collection_id, slot_type)
            selected_channel_ids.extend(supported_ids)
            selected_names.append(str(collection["name"]))
        selected_channel_ids = list(dict.fromkeys(selected_channel_ids))
        payload["selected_collection_ids"] = selected_ids
        payload["selected_collection_names"] = selected_names
        payload["selected_channel_ids"] = selected_channel_ids
        if selected_channel_ids:
            first_channel = self.channels.get_channel(conn, selected_channel_ids[0])
            payload["channel_id"] = first_channel["id"]
            payload["channel_title"] = self._global_channel_label(payload)
        else:
            payload.pop("channel_id", None)
            payload["channel_title"] = "待选择"

    def _global_placement_text(
        self,
        payload: dict[str, Any],
        panel: str,
        channels: list[Any] | None = None,
        collections: list[dict[str, Any]] | None = None,
    ) -> str:
        status_block = self._html_pre(
            [
                f"广告素材：{self._placement_creative_label(payload)}",
                f"插播位置：{self._placement_display_label(payload)}",
                f"频道：{self._global_channel_label(payload)}",
            ]
        )
        lines = [
            "<b>➕ 广告投放</b>",
            "",
            status_block,
            "",
            f"<b>{self._h(self._global_placement_step_title(panel, payload))}</b>",
        ]
        if panel == "creative":
            lines.append(self._html_quote("先选择要投放的广告素材；没有素材就先创建一套。"))
        elif panel == "display":
            lines.append(self._html_quote("选择广告在频道里的呈现方式。这里只能选一种。"))
        elif panel == "channel":
            if (payload.get("channel_pick_mode") or "folders") == "folders":
                selected_count = len(payload.get("selected_collection_ids") or [])
                selected_channels = len(payload.get("selected_channel_ids") or [])
                lines.extend(
                    [
                        self._html_quote(
                            "默认按频道文件夹投放。可以同时选择多个文件夹。\n"
                            "没有合适文件夹时，先去频道广场收藏频道；也可以临时按单个频道选择。"
                        ),
                        "",
                        f"<b>频道文件夹</b>  <code>已选 {selected_count} 个｜可投 {selected_channels} 个频道</code>",
                    ]
                )
                if collections:
                    for index, collection in enumerate(collections, start=1):
                        lines.append(
                            f"{index}. <b>{self._h(collection['name'])}</b>｜"
                            f"收藏 {collection['channel_count']}｜可投 {collection['supported_count']}"
                        )
                else:
                    lines.append(self._html_quote("还没有频道文件夹。可以新建文件夹，然后去频道广场收藏频道。"))
            elif channels:
                offset = int(payload.get("channel_offset") or 0)
                total = int(payload.get("channel_total") or len(channels))
                lines.extend(
                    [
                        self._html_quote(
                            f"选择要投放的频道。当前显示 {offset + 1}-{offset + len(channels)} / {total}。\n"
                            "清单里的链接用于预览频道；下方编号按钮用于勾选。"
                        ),
                        "",
                        "<b>频道清单</b>",
                    ]
                )
                for index, channel in enumerate(channels, start=1):
                    lines.extend(self._global_channel_summary_lines(index, channel))
            else:
                lines.append(self._html_quote("当前没有找到支持这个插播位置的频道，可以换一种插播位置。"))
        return "\n".join(lines)

    def _global_placement_keyboard(
        self,
        payload: dict[str, Any],
        panel: str,
        creatives: list[Any] | None = None,
        channels: list[Any] | None = None,
        collections: list[dict[str, Any]] | None = None,
    ) -> list[list[dict[str, str]]]:
        if panel == "creative":
            keyboard: list[list[dict[str, str]]] = []
            for index, creative in enumerate(creatives or []):
                label = self._short_title((creative["campaign_name"] or creative["text"] or "").replace("\n", " "), 18)
                keyboard.append([{"text": f"📄 {label}", "callback_data": f"launch:pick:{index}"}])
            keyboard.append([{"text": "➕ 添加广告素材", "callback_data": "launch:new"}])
            keyboard.append([{"text": "🔎 先挑频道", "callback_data": "market:home"}, {"text": "⭐ 频道收藏夹", "callback_data": "market:folders"}])
            keyboard.append([{"text": "🏠 返回主菜单", "callback_data": "menu:home"}])
            return keyboard
        if panel == "display":
            selected = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            keyboard = [
                [
                    {"text": self._placement_slot_button_text("button_tail", selected), "callback_data": "launch:slot:button_tail"},
                    {"text": self._placement_slot_button_text("light_tail", selected), "callback_data": "launch:slot:light_tail"},
                ],
                [
                    {"text": self._placement_slot_button_text("standard_card", selected), "callback_data": "launch:slot:standard_card"},
                    {"text": self._placement_slot_button_text("strong_post", selected), "callback_data": "launch:slot:strong_post"},
                ],
            ]
            keyboard.append([{"text": "⬅️ 上一步", "callback_data": "launch:back"}, {"text": "下一步 ➡️", "callback_data": "launch:next"}])
            return keyboard
        if panel == "channel":
            if (payload.get("channel_pick_mode") or "folders") == "folders":
                selected_collection_ids = set(str(collection_id) for collection_id in payload.get("selected_collection_ids") or [])
                keyboard = [
                    [
                        {"text": "📺 按频道选择", "callback_data": "launch:channel_mode:channels"},
                        {"text": "➕ 新建文件夹", "callback_data": "launch:new_folder"},
                    ]
                ]
                folder_buttons = [
                    {
                        "text": self._global_collection_button_text(index + 1, collection, selected=str(collection["id"]) in selected_collection_ids),
                        "callback_data": f"launch:folder:{index}",
                    }
                    for index, collection in enumerate(collections or [])
                ]
                keyboard.extend(self._button_grid(folder_buttons, 2))
                selected_channel_count = len(payload.get("selected_channel_ids") or [])
                if selected_channel_count:
                    keyboard.append([{"text": f"下一步 ➡️（{selected_channel_count} 个频道）", "callback_data": "launch:start"}])
                keyboard.append([{"text": "⬅️ 上一步", "callback_data": "launch:back"}, {"text": "🔎 频道广场", "callback_data": "market:home"}])
                return keyboard
            selected_ids = set(str(channel_id) for channel_id in payload.get("selected_channel_ids") or [])
            select_buttons = [
                {
                    "text": self._global_channel_select_button_text(index + 1, channel, selected=str(channel["id"]) in selected_ids),
                    "callback_data": f"launch:toggle:{index}",
                }
                for index, channel in enumerate(channels or [])
            ]
            keyboard = [[{"text": "📁 按文件夹选择", "callback_data": "launch:channel_mode:folders"}]]
            keyboard.extend(self._button_grid(select_buttons, 2))
            offset = int(payload.get("channel_offset") or 0)
            total = int(payload.get("channel_total") or len(channels or []))
            page_buttons: list[dict[str, str]] = []
            if offset > 0:
                page_buttons.append({"text": "⬅️ 上一批", "callback_data": "launch:page:prev"})
            if offset + len(channels or []) < total:
                page_buttons.append({"text": "下一批 ➡️", "callback_data": "launch:page:next"})
            if page_buttons:
                keyboard.append(page_buttons)
            selected_count = len(selected_ids)
            if selected_count:
                keyboard.append([{"text": f"✅ 开始投放（已选 {selected_count} 个）", "callback_data": "launch:start"}])
            keyboard.append([{"text": "⬅️ 上一步", "callback_data": "launch:back"}])
            return keyboard
        return [[{"text": "🏠 返回主菜单", "callback_data": "menu:home"}]]

    def _global_placement_step_title(self, panel: str, payload: dict[str, Any] | None = None) -> str:
        titles = {
            "creative": "第 1/5 步：选择广告素材",
            "display": "第 2/5 步：选择插播位置",
            "channel": "第 3/5 步：选择投放频道",
        }
        if panel == "channel" and payload and (payload.get("channel_pick_mode") or "folders") == "folders":
            return "第 3/5 步：选择频道文件夹"
        return titles.get(panel, titles["creative"])

    def _global_channel_label(self, payload: dict[str, Any]) -> str:
        selected = payload.get("selected_channel_ids") or []
        selected_collections = payload.get("selected_collection_names") or []
        if selected_collections:
            if len(selected_collections) == 1:
                return f"{selected_collections[0]}（{len(selected)} 个频道）"
            return f"已选 {len(selected_collections)} 个文件夹 / {len(selected)} 个频道"
        if selected and payload.get("selected_collection_name"):
            return f"{payload['selected_collection_name']}（{len(selected)} 个）"
        if len(selected) > 1:
            return f"已选 {len(selected)} 个"
        return payload.get("channel_title") or ("已选 1 个" if selected else "待选择")

    def _global_channel_summary_lines(self, index: int, channel: Any) -> list[str]:
        subscribers = self._compact_count(int(channel["subscribers"] or 0))
        title = self._short_title(str(channel["title"] or "未命名频道"), 16)
        price = cents_to_money(int(channel["unit_price_cents"] or 0))
        link = self._channel_public_url(channel)
        if link:
            return [f"<b>{index}. 👥 {self._h(subscribers)}｜{self._h(title)}｜USD {price}</b>", f'   <a href="{self._h(link)}">{self._h(link)}</a>']
        return [f"<b>{index}. 👥 {self._h(subscribers)}｜{self._h(title)}｜USD {price}</b>", "   暂无公开链接"]

    def _global_channel_select_button_text(self, index: int, channel: Any, *, selected: bool) -> str:
        title = self._short_title(str(channel["title"] or "未命名频道"), 9)
        return f"{'✅ ' if selected else ''}{index} {title}"

    def _global_collection_button_text(self, index: int, collection: dict[str, Any], *, selected: bool) -> str:
        title = self._short_title(str(collection["name"] or "未命名文件夹"), 8)
        supported = int(collection.get("supported_count") or 0)
        return f"{'✅ ' if selected else ''}{index} {title}（{supported}）"

    def _channel_public_url(self, channel: Any) -> str | None:
        username = str(channel["username"] or "").strip().lstrip("@")
        if username:
            return f"https://t.me/{username}"
        return None

    def _compact_count(self, value: int) -> str:
        value = max(0, value)
        if value >= 10000:
            return f"{value / 10000:.1f}万"
        if value:
            return str(value)
        return "未知"

    def _send_placement_configurator(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        panel: str = "creative",
    ) -> None:
        if panel == "home":
            panel = "creative"
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
        panel = self._placement_effective_panel(panel, payload)
        with self.db.transaction() as conn:
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
            text=self._placement_text(channel, payload, panel, account),
            inline_keyboard=self._placement_keyboard(panel, payload, creatives, account),
            parse_mode="HTML",
        )

    def _handle_placement_callback(self, data: str, chat_id: str | int, user: dict[str, Any], message: dict[str, Any] | None = None) -> dict[str, Any]:
        if data == "place:home":
            state = self._get_conversation(chat_id)
            if state and state["flow"] == "placement_config":
                payload = json.loads(state["payload_json"] or "{}")
                if payload.get("launch_mode") == "global":
                    self._send_global_placement(chat_id, user, message, payload=payload, panel="creative")
                    return {"handled": True, "type": "callback_global_placement_home"}
            self._send_placement_configurator(chat_id, user, message, panel="creative")
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

        if data == "place:next":
            current_panel = state.get("step") or "creative"
            next_panel = self._placement_next_panel(payload, current_panel)
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel=next_panel)
            return {"handled": True, "type": "callback_placement_next", "panel": next_panel}

        if data == "place:back":
            current_panel = state.get("step") or "creative"
            previous_panel = self._placement_previous_panel(payload, current_panel)
            if previous_panel == "channel":
                self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
                return {"handled": True, "type": "callback_placement_back", "panel": previous_panel}
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel=previous_panel)
            return {"handled": True, "type": "callback_placement_back", "panel": previous_panel}

        if data == "place:asset_back":
            current_step = state.get("step") or "ad_name"
            previous_step = self._placement_asset_previous_step(current_step)
            if not previous_step:
                self._reply_or_edit(
                    chat_id=chat_id,
                    source_message=message,
                    text=self._placement_creative_prompt(payload, current_step),
                    inline_keyboard=self._placement_asset_keyboard(current_step),
                )
                return {"handled": True, "type": "callback_placement_asset_back", "step": current_step}
            self._clear_placement_asset_from_step(payload, previous_step)
            self._set_conversation(chat_id, state["account_id"], "placement_config", previous_step, payload)
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text=self._placement_creative_prompt(payload, previous_step),
                inline_keyboard=self._placement_asset_keyboard(previous_step),
            )
            return {"handled": True, "type": "callback_placement_asset_back", "step": previous_step}

        if data.startswith("place:slot:"):
            previous_slot = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            slot_type = self.channels.normalize_slot_type(data.removeprefix("place:slot:"))
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
            target_panel = state.get("step") or "display"
            if target_panel not in {"schedule", "confirm"}:
                target_panel = "display"
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel=target_panel)
            return {"handled": True, "type": "callback_placement_pin", "pin": bool(payload.get("pin"))}

        if data.startswith("place:pin:"):
            slot_type = self.channels.normalize_slot_type(data.removeprefix("place:pin:"))
            if slot_type in PINNABLE_PLACEMENT_SLOTS:
                if payload.get("slot_type") != slot_type:
                    payload["slot_type"] = slot_type
                    payload["pin"] = True
                else:
                    payload["pin"] = not bool(payload.get("pin"))
            target_panel = state.get("step") or "display"
            if target_panel not in {"schedule", "confirm"}:
                target_panel = "display"
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel=target_panel)
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
                creative = conn.execute(
                    """
                    SELECT cr.*, ca.name AS campaign_name
                    FROM creatives cr
                    JOIN campaigns ca ON ca.id = cr.campaign_id
                    WHERE cr.id = ?
                    """,
                    (creative_ids[index],),
                ).fetchone()
            if not creative or creative["archived_at"]:
                self._reply_or_edit(chat_id=chat_id, source_message=message, text="⚠️ 广告素材已不可用。", inline_keyboard=[[{"text": "📁 广告素材", "callback_data": "place:creative"}]])
                return {"handled": True, "type": "callback_placement_creative_missing"}
            payload.update(
                {
                    "material_id": creative["id"],
                    "creative_text": creative["text"],
                    "creative_name": creative["campaign_name"] or "广告库素材",
                    "target_url": creative["target_url"],
                    "button_text": creative["button_text"],
                    "light_short_text": creative["light_short_text"] or creative["short_text"] or creative["button_text"],
                    "standard_text": creative["standard_text"] or creative["text"],
                    "media_file_id": creative["media_file_id"],
                    "media_type": creative["media_type"],
                    "selected_creative_id": creative["id"],
                }
            )
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel="display")
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
                for key in ("material_id", "selected_creative_id", "creative_text", "target_url", "button_text", "light_short_text", "standard_text", "media_file_id", "media_type"):
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

    def _handle_channel_market_callback(self, data: str, chat_id: str | int, user: dict[str, Any], message: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self._get_conversation(chat_id)
        payload = json.loads(state["payload_json"] or "{}") if state and state["flow"] == "channel_market" else {}

        if data == "market:home":
            self._send_channel_market_home(chat_id, user, message)
            return {"handled": True, "type": "callback_channel_market_browse"}

        if data == "market:browse":
            self._send_channel_market_browse(chat_id, user, message, payload=payload)
            return {"handled": True, "type": "callback_channel_market_browse"}

        if data in {"market:next", "market:prev"}:
            offset = int(payload.get("offset") or 0)
            offset = offset + 1 if data == "market:next" else offset - 1
            self._send_channel_market_browse(chat_id, user, message, payload=payload, offset=max(0, offset))
            return {"handled": True, "type": "callback_channel_market_page", "offset": max(0, offset)}

        if data == "market:save":
            channel_id = payload.get("channel_id")
            user_id = user.get("id") or chat_id
            with self.db.transaction() as conn:
                account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
                collection = self._channel_collection_by_id_conn(conn, payload.get("collection_id") or "")
                if not collection or collection["advertiser_account_id"] != account["id"]:
                    collection = self._ensure_channel_collection_conn(conn, account["id"], "默认收藏夹")
                if channel_id:
                    self._save_channel_to_collection_conn(conn, account["id"], collection["id"], str(channel_id))
                    payload["collection_id"] = collection["id"]
                    payload["collection_name"] = collection["name"]
                    self._set_conversation_conn(conn, chat_id, account["id"], "channel_market", "browse", payload)
            self._send_channel_market_browse(chat_id, user, message, payload=payload)
            return {"handled": True, "type": "callback_channel_market_saved", "channel_id": channel_id}

        if data == "market:folders":
            self._send_channel_collections(chat_id, user, message)
            return {"handled": True, "type": "callback_channel_market_folders"}

        if data == "market:new_folder":
            user_id = user.get("id") or chat_id
            with self.db.transaction() as conn:
                account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
                self._set_conversation_conn(conn, chat_id, account["id"], "channel_market", "folder_name", payload)
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text="➕ 新建频道收藏夹\n\n发送收藏夹名称，例如：动漫频道、工具号、成人用品。",
                inline_keyboard=[[{"text": "⭐ 收藏夹列表", "callback_data": "market:folders"}, {"text": "↩️ 返回广场", "callback_data": "market:browse"}]],
            )
            return {"handled": True, "type": "callback_channel_market_new_folder"}

        if data.startswith("market:folder:"):
            index = int(data.removeprefix("market:folder:"))
            collection_ids = payload.get("collection_ids") or []
            if index < 0 or index >= len(collection_ids):
                self._send_channel_collections(chat_id, user, message)
                return {"handled": True, "type": "callback_channel_market_folder_missing"}
            user_id = user.get("id") or chat_id
            with self.db.transaction() as conn:
                account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
                collection = self._channel_collection_by_id_conn(conn, str(collection_ids[index]))
                if not collection or collection["advertiser_account_id"] != account["id"]:
                    self._send_channel_collections(chat_id, user, message)
                    return {"handled": True, "type": "callback_channel_market_folder_missing"}
                total = self._market_channel_count_conn(conn)
                offset = int(payload.get("offset") or 0)
                if total and offset >= total:
                    offset = max(0, total - 1)
                payload["collection_id"] = collection["id"]
                payload["collection_name"] = collection["name"]
                payload["offset"] = offset
                self._set_conversation_conn(conn, chat_id, account["id"], "channel_market", "browse", payload)
            self._send_channel_market_browse(chat_id, user, message, payload=payload)
            return {"handled": True, "type": "callback_channel_market_folder_selected", "collection_id": collection_ids[index]}

        if data.startswith("market:launch_folder:"):
            index = int(data.removeprefix("market:launch_folder:"))
            collection_ids = payload.get("collection_ids") or []
            collection_id = payload.get("collection_id")
            if collection_ids and 0 <= index < len(collection_ids):
                collection_id = collection_ids[index]
            if not collection_id:
                self._send_channel_collections(chat_id, user, message)
                return {"handled": True, "type": "callback_channel_market_folder_missing"}
            return self._start_global_placement_from_collection(chat_id, user, message, str(collection_id))

        self._send_channel_market_home(chat_id, user, message)
        return {"handled": True, "type": "callback_channel_market_unknown"}

    def _start_global_placement_from_collection(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None,
        collection_id: str,
    ) -> dict[str, Any]:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            collection = self._channel_collection_by_id_conn(conn, collection_id)
            if not collection or collection["advertiser_account_id"] != account["id"]:
                self._send_channel_collections(chat_id, user, source_message)
                return {"handled": True, "type": "callback_channel_market_folder_missing"}
            channels = self._channel_collection_channels_conn(conn, collection_id, limit=200)
        if not channels:
            self._send_channel_collection_detail(chat_id, user, source_message, collection_id=collection_id)
            return {"handled": True, "type": "callback_channel_market_folder_empty"}
        payload = self._default_global_placement_payload()
        payload["selected_collection_id"] = collection_id
        payload["selected_collection_name"] = collection["name"]
        payload["selected_channel_ids"] = [str(channel["id"]) for channel in channels]
        payload["channel_id"] = str(channels[0]["id"])
        payload["channel_title"] = f"{collection['name']}（{len(channels)} 个）"
        self._send_global_placement(chat_id, user, source_message, payload=payload, panel="creative")
        return {"handled": True, "type": "callback_channel_market_launch_collection", "collection_id": collection_id, "channel_count": len(channels)}

    def _handle_global_placement_callback(self, data: str, chat_id: str | int, user: dict[str, Any], message: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self._get_conversation(chat_id)
        if not state or state["flow"] != "global_placement":
            self._send_global_placement(chat_id, user, message, panel="creative")
            return {"handled": True, "type": "callback_global_placement_started"}
        payload = json.loads(state["payload_json"] or "{}")
        current_panel = state.get("step") or "creative"

        if data == "launch:new":
            self._begin_placement_creative(chat_id, user, message, payload, state["account_id"])
            return {"handled": True, "type": "callback_global_placement_new_creative"}

        if data.startswith("launch:pick:"):
            index = int(data.removeprefix("launch:pick:"))
            creative_ids = payload.get("creative_ids") or []
            if index < 0 or index >= len(creative_ids):
                self._send_global_placement(chat_id, user, message, payload=payload, panel="creative")
                return {"handled": True, "type": "callback_global_placement_creative_missing"}
            with self.db.transaction() as conn:
                creative = conn.execute(
                    """
                    SELECT cr.*, ca.name AS campaign_name
                    FROM creatives cr
                    JOIN campaigns ca ON ca.id = cr.campaign_id
                    WHERE cr.id = ?
                    """,
                    (creative_ids[index],),
                ).fetchone()
            if not creative or creative["archived_at"]:
                self._send_global_placement(chat_id, user, message, payload=payload, panel="creative")
                return {"handled": True, "type": "callback_global_placement_creative_missing"}
            payload.update(
                {
                    "material_id": creative["id"],
                    "creative_text": creative["text"],
                    "creative_name": creative["campaign_name"],
                    "target_url": creative["target_url"],
                    "button_text": creative["button_text"],
                    "light_short_text": creative["light_short_text"] or creative["short_text"] or creative["button_text"],
                    "standard_text": creative["standard_text"] or creative["text"],
                    "media_file_id": creative["media_file_id"],
                    "media_type": creative["media_type"],
                    "selected_creative_id": creative["id"],
                }
            )
            self._send_global_placement(chat_id, user, message, payload=payload, panel="display")
            return {"handled": True, "type": "callback_global_placement_creative_selected"}

        if data.startswith("launch:slot:"):
            slot_type = self.channels.normalize_slot_type(data.removeprefix("launch:slot:"))
            payload["slot_type"] = slot_type
            payload["pin"] = False
            if payload.get("selected_collection_id"):
                with self.db.transaction() as conn:
                    selected_ids = self._collection_supported_channel_ids_conn(conn, str(payload["selected_collection_id"]), slot_type)
                    first_channel = self.channels.get_channel(conn, selected_ids[0]) if selected_ids else None
                if selected_ids and first_channel:
                    payload["selected_channel_ids"] = selected_ids
                    payload["channel_id"] = first_channel["id"]
                    collection_name = payload.get("selected_collection_name") or "频道收藏夹"
                    payload["channel_title"] = f"{collection_name}（{len(selected_ids)} 个）"
                    next_panel = "schedule" if slot_type in SCHEDULED_PLACEMENT_SLOTS else "confirm"
                    self._send_placement_configurator(chat_id, user, message, payload=payload, panel=next_panel)
                    return {"handled": True, "type": "callback_global_placement_collection_slot", "slot_type": slot_type, "channel_count": len(selected_ids), "panel": next_panel}
                payload.pop("channel_id", None)
                payload.pop("channel_title", None)
                payload["selected_channel_ids"] = []
            else:
                payload.pop("channel_id", None)
                payload.pop("channel_title", None)
                payload["selected_channel_ids"] = []
                payload["selected_collection_ids"] = []
                payload["selected_collection_names"] = []
                payload["channel_pick_mode"] = "folders"
            payload["channel_offset"] = 0
            self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
            return {"handled": True, "type": "callback_global_placement_slot", "slot_type": slot_type}

        if data == "launch:next":
            if current_panel == "display" and payload.get("slot_type"):
                self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
                return {"handled": True, "type": "callback_global_placement_next", "panel": "channel"}
            self._send_global_placement(chat_id, user, message, payload=payload, panel=current_panel)
            return {"handled": True, "type": "callback_global_placement_next_blocked", "panel": current_panel}

        if data == "launch:back":
            previous_panel = "creative" if current_panel == "display" else "display" if current_panel == "channel" else "creative"
            self._send_global_placement(chat_id, user, message, payload=payload, panel=previous_panel)
            return {"handled": True, "type": "callback_global_placement_back", "panel": previous_panel}

        if data == "launch:channel_mode:channels":
            payload["channel_pick_mode"] = "channels"
            payload["channel_offset"] = 0
            payload["selected_collection_ids"] = []
            payload["selected_collection_names"] = []
            payload["selected_channel_ids"] = []
            payload.pop("channel_id", None)
            payload.pop("channel_title", None)
            self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
            return {"handled": True, "type": "callback_global_placement_channel_mode", "mode": "channels"}

        if data == "launch:channel_mode:folders":
            payload["channel_pick_mode"] = "folders"
            payload["channel_offset"] = 0
            payload["selected_channel_ids"] = []
            payload.pop("channel_id", None)
            payload.pop("channel_title", None)
            self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
            return {"handled": True, "type": "callback_global_placement_channel_mode", "mode": "folders"}

        if data == "launch:new_folder":
            self._set_conversation(chat_id, state["account_id"], "global_placement", "folder_name", payload)
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=message,
                text="<b>➕ 新建频道文件夹</b>\n\n" + self._html_quote("发送文件夹名称，例如：国漫、工具号、成人用品。创建后会带你去频道广场收藏频道。"),
                inline_keyboard=[[{"text": "⬅️ 返回选择文件夹", "callback_data": "launch:channel_mode:folders"}]],
                parse_mode="HTML",
            )
            return {"handled": True, "type": "callback_global_placement_new_folder"}

        if data.startswith("launch:folder:"):
            index = int(data.removeprefix("launch:folder:"))
            collection_ids = payload.get("collection_ids") or []
            if index < 0 or index >= len(collection_ids):
                self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
                return {"handled": True, "type": "callback_global_placement_folder_missing"}
            target_id = str(collection_ids[index])
            selected_collection_ids = [str(collection_id) for collection_id in payload.get("selected_collection_ids") or []]
            if target_id in selected_collection_ids:
                selected_collection_ids = [collection_id for collection_id in selected_collection_ids if collection_id != target_id]
            else:
                selected_collection_ids.append(target_id)
            with self.db.transaction() as conn:
                self._apply_global_collection_selection_conn(conn, payload, selected_collection_ids)
            payload["channel_pick_mode"] = "folders"
            self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
            return {
                "handled": True,
                "type": "callback_global_placement_folder_toggled",
                "selected_folder_count": len(payload.get("selected_collection_ids") or []),
                "selected_count": len(payload.get("selected_channel_ids") or []),
            }

        if data.startswith("launch:page:"):
            direction = data.removeprefix("launch:page:")
            offset = int(payload.get("channel_offset") or 0)
            total = int(payload.get("channel_total") or 0)
            if direction == "next":
                offset += 10
            elif direction == "prev":
                offset -= 10
            if total and offset >= total:
                offset = max(0, ((total - 1) // 10) * 10)
            payload["channel_offset"] = max(0, offset)
            self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
            return {"handled": True, "type": "callback_global_placement_channel_page", "offset": payload["channel_offset"]}

        if data.startswith("launch:toggle:"):
            index = int(data.removeprefix("launch:toggle:"))
            channel_ids = payload.get("channel_ids") or []
            if index < 0 or index >= len(channel_ids):
                self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
                return {"handled": True, "type": "callback_global_placement_channel_missing"}
            target_id = str(channel_ids[index])
            selected_ids = [str(channel_id) for channel_id in payload.get("selected_channel_ids") or []]
            if target_id in selected_ids:
                selected_ids = [channel_id for channel_id in selected_ids if channel_id != target_id]
            else:
                selected_ids.append(target_id)
            payload["selected_channel_ids"] = selected_ids
            payload["selected_collection_ids"] = []
            payload["selected_collection_names"] = []
            if selected_ids:
                with self.db.transaction() as conn:
                    channel = self.channels.get_channel(conn, selected_ids[0])
                payload["channel_id"] = channel["id"]
                payload["channel_title"] = channel["title"] if len(selected_ids) == 1 else f"已选 {len(selected_ids)} 个"
            else:
                payload.pop("channel_id", None)
                payload.pop("channel_title", None)
            self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
            return {"handled": True, "type": "callback_global_placement_channel_toggled", "selected_count": len(selected_ids)}

        if data == "launch:start":
            selected_ids = self._placement_channel_ids(payload)
            if not selected_ids:
                self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
                return {"handled": True, "type": "callback_global_placement_channel_required"}
            with self.db.transaction() as conn:
                channel = self.channels.get_channel(conn, selected_ids[0])
            payload["selected_channel_ids"] = selected_ids
            payload["channel_id"] = channel["id"]
            payload["channel_title"] = self._global_channel_label(payload) if payload.get("selected_collection_names") else (channel["title"] if len(selected_ids) == 1 else f"已选 {len(selected_ids)} 个")
            slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            next_panel = "schedule" if slot_type in SCHEDULED_PLACEMENT_SLOTS else "confirm"
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel=next_panel)
            return {"handled": True, "type": "callback_global_placement_channel_selected", "channel_ids": selected_ids, "panel": next_panel}

        if data.startswith("launch:channel:"):
            index = int(data.removeprefix("launch:channel:"))
            channel_ids = payload.get("channel_ids") or []
            if index < 0 or index >= len(channel_ids):
                self._send_global_placement(chat_id, user, message, payload=payload, panel="channel")
                return {"handled": True, "type": "callback_global_placement_channel_missing"}
            with self.db.transaction() as conn:
                channel = self.channels.get_channel(conn, channel_ids[index])
            payload["channel_id"] = channel["id"]
            payload["channel_title"] = channel["title"]
            payload["selected_channel_ids"] = [str(channel["id"])]
            slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            next_panel = "schedule" if slot_type in SCHEDULED_PLACEMENT_SLOTS else "confirm"
            self._send_placement_configurator(chat_id, user, message, payload=payload, panel=next_panel)
            return {"handled": True, "type": "callback_global_placement_channel_selected", "channel_id": channel["id"], "panel": next_panel}

        self._send_global_placement(chat_id, user, message, payload=payload, panel=current_panel)
        return {"handled": True, "type": "callback_global_placement_unknown"}

    def _handle_placement_message(self, message: dict[str, Any], state: dict[str, Any], text: str) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        if not chat_id:
            return {"handled": False, "reason": "missing_chat"}
        payload = json.loads(state["payload_json"] or "{}")
        clean_text = text.strip()
        step = state["step"]

        if step == "time_input":
            if not clean_text:
                self.gateway.send_private_message(chat_id=chat_id, text="请发送发布时间文字，或点击取消。", inline_keyboard=self._cancel_keyboard("取消设置", "place:home"))
                return {"handled": True, "type": "placement_text_required"}
            payload["scheduled_label"] = clean_text
            self._send_placement_configurator(chat_id, user, None, payload=payload, panel="schedule")
            return {"handled": True, "type": "placement_time_saved"}

        if step == "ad_name":
            if len(clean_text) < 2 or len(clean_text) > 24:
                self.gateway.send_private_message(chat_id=chat_id, text="广告名称需要 2-24 个字，方便你下次复用。", inline_keyboard=self._placement_asset_keyboard(step))
                return {"handled": True, "type": "placement_invalid_ad_name"}
            payload["creative_name"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "placement_config", "media_detail", payload)
            self.gateway.send_private_message(chat_id=chat_id, text=self._placement_creative_prompt(payload, "media_detail"), inline_keyboard=self._placement_asset_keyboard("media_detail"))
            return {"handled": True, "type": "placement_ad_name_saved"}

        if step == "media_detail":
            media = self._message_media(message)
            if not media:
                self.gateway.send_private_message(chat_id=chat_id, text="媒体文件需要发送一张图片或一个视频。详细介绍下一步再填。", inline_keyboard=self._placement_asset_keyboard(step))
                return {"handled": True, "type": "placement_media_required"}
            payload["media_file_id"] = media["file_id"]
            payload["media_type"] = media["media_type"]
            if clean_text:
                if len(clean_text) < 4 or len(clean_text) > 2000:
                    self.gateway.send_private_message(chat_id=chat_id, text="详细介绍需要 4-2000 个字。", inline_keyboard=self._placement_asset_keyboard(step))
                    return {"handled": True, "type": "placement_invalid_detail"}
                payload["creative_text"] = clean_text
                self._set_conversation(chat_id, state["account_id"], "placement_config", "target_url", payload)
                self.gateway.send_private_message(chat_id=chat_id, text=self._placement_creative_prompt(payload, "target_url"), inline_keyboard=self._placement_asset_keyboard("target_url"))
                return {"handled": True, "type": "placement_media_detail_saved"}
            self._set_conversation(chat_id, state["account_id"], "placement_config", "detail_text", payload)
            self.gateway.send_private_message(chat_id=chat_id, text=self._placement_creative_prompt(payload, "detail_text"), inline_keyboard=self._placement_asset_keyboard("detail_text"))
            return {"handled": True, "type": "placement_media_saved"}

        if step == "detail_text":
            if len(clean_text) < 4 or len(clean_text) > 2000:
                self.gateway.send_private_message(chat_id=chat_id, text="详细介绍需要 4-2000 个字。", inline_keyboard=self._placement_asset_keyboard(step))
                return {"handled": True, "type": "placement_invalid_detail"}
            payload["creative_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "placement_config", "target_url", payload)
            self.gateway.send_private_message(chat_id=chat_id, text=self._placement_creative_prompt(payload, "target_url"), inline_keyboard=self._placement_asset_keyboard("target_url"))
            return {"handled": True, "type": "placement_detail_saved"}

        if step == "target_url":
            if not (clean_text.startswith("https://") or clean_text.startswith("http://")):
                self.gateway.send_private_message(chat_id=chat_id, text="跳转链接格式不对。请发送以 http:// 或 https:// 开头的链接。", inline_keyboard=self._placement_asset_keyboard(step))
                return {"handled": True, "type": "placement_invalid_url"}
            payload["target_url"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "placement_config", "button_text", payload)
            self.gateway.send_private_message(chat_id=chat_id, text=self._placement_creative_prompt(payload, "button_text"), inline_keyboard=self._placement_asset_keyboard("button_text"))
            return {"handled": True, "type": "placement_url_saved"}

        if step == "button_text":
            if len(clean_text) < 2 or len(clean_text) > 5:
                self.gateway.send_private_message(
                    chat_id=chat_id,
                    text="按钮名称需要 2-5 个字。这里只填按钮上显示的文字，例如：咨询、查看、领取、下单。",
                    inline_keyboard=self._placement_asset_keyboard(step),
                )
                return {"handled": True, "type": "placement_invalid_button_text"}
            payload["button_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "placement_config", "short_text", payload)
            self.gateway.send_private_message(chat_id=chat_id, text=self._placement_creative_prompt(payload, "short_text"), inline_keyboard=self._placement_asset_keyboard("short_text"))
            return {"handled": True, "type": "placement_button_text_saved"}

        if step == "short_text":
            if len(clean_text) < 2 or len(clean_text) > 15:
                self.gateway.send_private_message(chat_id=chat_id, text="一句话广告需要 2-15 个字，会用于文字插播入口。", inline_keyboard=self._placement_asset_keyboard(step))
                return {"handled": True, "type": "placement_invalid_short_text"}
            payload["light_short_text"] = clean_text
            self._set_conversation(chat_id, state["account_id"], "placement_config", "standard_text", payload)
            self.gateway.send_private_message(chat_id=chat_id, text=self._placement_creative_prompt(payload, "standard_text"), inline_keyboard=self._placement_asset_keyboard("standard_text"))
            return {"handled": True, "type": "placement_short_text_saved"}

        if step == "standard_text":
            if len(clean_text) < 4 or len(clean_text) > 220 or clean_text.count("\n") > 4:
                self.gateway.send_private_message(chat_id=chat_id, text="简短文案请控制在 4-220 个字、最多 5 行。", inline_keyboard=self._placement_asset_keyboard(step))
                return {"handled": True, "type": "placement_invalid_standard_text"}
            payload["standard_text"] = clean_text
            with self.db.transaction() as conn:
                self._save_placement_creative_conn(conn, state["account_id"], payload)
            if payload.get("launch_mode") == "global":
                self._send_global_placement(chat_id, user, None, payload=payload, panel="display")
            else:
                self._send_placement_configurator(chat_id, user, None, payload=payload, panel="display")
            return {"handled": True, "type": "placement_standard_text_saved"}

        return {"handled": False, "reason": "unknown_placement_step"}

    def _begin_placement_creative(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None,
        payload: dict[str, Any],
        account_id: str | None,
    ) -> None:
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        if slot_type:
            payload["slot_type"] = slot_type
        self._set_conversation(chat_id, account_id, "placement_config", "ad_name", payload)
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=self._placement_creative_prompt(payload, "ad_name"),
            inline_keyboard=self._placement_asset_keyboard("ad_name"),
        )

    def _placement_creative_prompt(self, payload: dict[str, Any], step: str) -> str:
        labels = [
            ("creative_name", "广告名称"),
            ("media_file_id", "媒体文件"),
            ("creative_text", "详细介绍"),
            ("target_url", "跳转链接"),
            ("button_text", "按钮名称"),
            ("light_short_text", "一句话广告"),
            ("standard_text", "简短文案"),
        ]
        def is_done(key: str) -> bool:
            value = payload.get(key)
            if key == "button_text":
                return bool(value) and value not in {"打开链接", "查看详情"}
            return bool(value)

        done = sum(1 for key, _ in labels if is_done(key))
        step_keys = {
            "ad_name": "creative_name",
            "media_detail": "media_file_id",
            "detail_text": "creative_text",
            "target_url": "target_url",
            "button_text": "button_text",
            "short_text": "light_short_text",
            "standard_text": "standard_text",
        }
        current_key = step_keys.get(step, "creative_name")
        current_index = next((index for index, (key, _) in enumerate(labels, start=1) if key == current_key), 1)
        current_label = dict(labels).get(current_key, "广告信息")
        prompts = {
            "ad_name": "先给这套广告起个名字，只有你自己看得到，方便以后复用。例：AA 成人用品、记账工具 A 版。",
            "media_detail": "请发送一张图片或一个视频。这一步只收媒体文件，详细介绍下一步填写。",
            "detail_text": "媒体已收到。请发送详细介绍，定制插播和广告详情页会使用这段内容。",
            "target_url": "发送跳转链接，必须以 http:// 或 https:// 开头。读者在广告详情里点击按钮会打开这个链接。",
            "button_text": "发送按钮名称，2-5 个字。这里只填按钮上显示的文字，例如：咨询、查看、领取、下单。",
            "short_text": "发送一句话广告，2-15 个字。它会显示在文字插播入口里，不是按钮名，也不是链接。",
            "standard_text": "发送简短文案，4-220 个字、最多 5 行。它会配合媒体和按钮组成较克制的标准插播。",
        }
        return "\n".join(
            [
                "🧩 创建广告资产",
                f"第 {current_index}/{len(labels)} 步",
                f"👉 当前填写：{current_label}",
                f"完成度：{done}/{len(labels)}",
                "",
                *self._placement_asset_status_lines(labels, payload, current_key),
                "",
                prompts.get(step, "请继续补充广告信息。"),
            ]
        )

    def _placement_asset_status_lines(self, labels: list[tuple[str, str]], payload: dict[str, Any], current_key: str) -> list[str]:
        lines: list[str] = []
        for key, label in labels:
            value = self._placement_asset_value_summary(key, payload)
            if key == current_key and not value:
                prefix = "👉"
            elif value:
                prefix = "✅"
            else:
                prefix = "⬜"
            suffix = f"：{value}" if value else ""
            lines.append(f"{prefix} {label}{suffix}")
        return lines

    def _placement_asset_value_summary(self, key: str, payload: dict[str, Any]) -> str:
        if key == "button_text" and payload.get(key) in {"打开链接", "查看详情"}:
            return ""
        if key == "media_file_id":
            if not payload.get("media_file_id"):
                return ""
            media_type = str(payload.get("media_type") or "")
            if media_type == "video":
                return "视频已收到"
            if media_type == "animation":
                return "动图已收到"
            return "图片已收到"
        value = str(payload.get(key) or "").replace("\n", " ").strip()
        if not value:
            return ""
        return self._short_title(value, 12)

    def _placement_asset_previous_step(self, step: str) -> str | None:
        try:
            index = PLACEMENT_ASSET_STEPS.index(step)
        except ValueError:
            return None
        if index <= 0:
            return None
        return PLACEMENT_ASSET_STEPS[index - 1]

    def _clear_placement_asset_from_step(self, payload: dict[str, Any], step: str) -> None:
        try:
            start_index = PLACEMENT_ASSET_STEPS.index(step)
        except ValueError:
            return
        for clear_step in PLACEMENT_ASSET_STEPS[start_index:]:
            for field in PLACEMENT_ASSET_STEP_FIELDS.get(clear_step, ()):
                payload.pop(field, None)

    def _placement_asset_keyboard(self, step: str) -> list[list[dict[str, str]]]:
        keyboard: list[list[dict[str, str]]] = []
        if self._placement_asset_previous_step(step):
            keyboard.append([{"text": "⬅️ 返回上一步", "callback_data": "place:asset_back"}])
        keyboard.append([{"text": "取消创建", "callback_data": "place:home"}])
        return keyboard

    def _message_media(self, message: dict[str, Any]) -> dict[str, str] | None:
        photos = message.get("photo") or []
        if photos:
            largest = photos[-1]
            file_id = largest.get("file_id")
            if file_id:
                return {"media_type": "photo", "file_id": file_id}
        for media_type in ["video", "animation"]:
            media = message.get(media_type) or {}
            file_id = media.get("file_id")
            if file_id:
                return {"media_type": media_type, "file_id": file_id}
        return None

    def _save_placement_creative_conn(self, conn: Any, account_id: str | None, payload: dict[str, Any]) -> None:
        if not account_id or payload.get("selected_creative_id"):
            return
        name = str(payload.get("creative_name") or "").strip() or "广告库素材"
        text = str(payload.get("creative_text") or "").strip()
        short_text = str(payload.get("light_short_text") or "").strip()
        standard_text = str(payload.get("standard_text") or "").strip()
        target_url = str(payload.get("target_url") or "").strip()
        button_text = str(payload.get("button_text") or "打开链接").strip() or "打开链接"
        media_file_id = str(payload.get("media_file_id") or "").strip()
        media_type = str(payload.get("media_type") or "").strip()
        if not text or not target_url:
            return
        if not short_text:
            short_text = self._short_title(button_text, 15)
            payload["light_short_text"] = short_text
        if not standard_text:
            standard_text = self._short_title(text.replace("\n", " "), 220)
            payload["standard_text"] = standard_text
        content_hash = hashlib.sha256(
            f"{name}|{short_text}|{standard_text}|{text}|{target_url}|{button_text}|{media_file_id}".encode("utf-8")
        ).hexdigest()
        existing = conn.execute(
            """
            SELECT cr.*
            FROM creatives cr
            JOIN campaigns ca ON ca.id = cr.campaign_id
            WHERE ca.advertiser_account_id = ?
              AND cr.content_hash = ?
              AND cr.status != 'rejected'
              AND cr.archived_at IS NULL
            ORDER BY cr.updated_at DESC, cr.created_at DESC
            LIMIT 1
            """,
            (account_id, content_hash),
        ).fetchone()
        if existing:
            payload["material_id"] = existing["id"]
            payload["selected_creative_id"] = existing["id"]
            payload["button_text"] = existing["button_text"]
            payload["light_short_text"] = existing["light_short_text"] or existing["short_text"] or short_text
            payload["standard_text"] = existing["standard_text"] or standard_text
            payload["media_file_id"] = existing["media_file_id"]
            payload["media_type"] = existing["media_type"]
            return
        campaign_id = new_id("camp")
        creative_id = new_id("cre")
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "standard_card")
        format_type = slot_type if slot_type in PLACEMENT_SLOT_TYPES else "standard_card"
        conn.execute(
            """
            INSERT INTO campaigns (id, advertiser_account_id, name)
            VALUES (?, ?, ?)
            """,
            (campaign_id, account_id, name),
        )
        conn.execute(
            """
            INSERT INTO creatives (
                id, campaign_id, advertiser_account_id, format_type,
                text, target_url, button_text, short_text, standard_text,
                media_file_id, media_type, category, light_short_text, content_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'general', ?, ?)
            """,
            (
                creative_id,
                campaign_id,
                account_id,
                format_type,
                text,
                target_url,
                button_text,
                short_text,
                standard_text,
                media_file_id or None,
                media_type or None,
                short_text,
                content_hash,
            ),
        )
        payload["material_id"] = creative_id
        payload["selected_creative_id"] = creative_id

    def _submit_placement_order(self, chat_id: str | int, user: dict[str, Any], source_message: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
        missing_panel = self._placement_missing_panel(payload)
        if missing_panel:
            self._send_placement_configurator(chat_id, user, source_message, payload=payload, panel=missing_panel)
            return {"handled": True, "type": "callback_placement_submit_incomplete", "missing": missing_panel}
        channel_ids = self._placement_channel_ids(payload)
        if not channel_ids:
            if payload.get("launch_mode") == "global":
                self._send_global_placement(chat_id, user, source_message, payload=payload, panel="channel")
                return {"handled": True, "type": "callback_placement_submit_incomplete", "missing": "channel"}
            self._send_placement_configurator(chat_id, user, source_message, payload=payload, panel="display")
            return {"handled": True, "type": "callback_placement_submit_incomplete", "missing": "channel"}

        user_id = user.get("id") or chat_id
        quote = self._placement_quote(payload)
        scheduled_at = datetime.now(timezone.utc)
        slot_type = self.channels.normalize_slot_type(payload["slot_type"])
        period_key = "once" if slot_type in CHANNEL_PACED_PLACEMENT_SLOTS else payload.get("period") or "once"
        period = PLACEMENT_PERIODS.get(period_key, PLACEMENT_PERIODS["once"])
        deliveries = int(period["deliveries"])
        end_at = scheduled_at + timedelta(days=deliveries - 1) if deliveries > 1 else None
        order_slot_type = "pin24h" if payload.get("pin") else self.channels.normalize_slot_type(payload["slot_type"])
        try:
            with self.db.transaction() as conn:
                account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user) or None)
                if int(account["available_balance_cents"] or 0) < quote["total_cents"]:
                    raise ChaboError("广告钱包余额不足，请先充值或减少投放频道。")
                channels = [self.channels.get_channel(conn, channel_id) for channel_id in channel_ids]
            orders = []
            for channel in channels:
                channel_quote = self._placement_quote_for_channel(str(channel["id"]), payload)
                creative_args = (
                    {"material_id": payload["material_id"]}
                    if payload.get("material_id")
                    else {
                        "text": payload["creative_text"],
                        "target_url": payload["target_url"],
                        "light_short_text": payload.get("light_short_text"),
                    }
                )
                order = self.orders.create_order(
                    advertiser_telegram_user_id=user_id,
                    channel_token=channel["ref_token"],
                    slot_type=order_slot_type,
                    button_text=payload.get("button_text") or "打开链接",
                    budget_cents=channel_quote["total_cents"],
                    scheduled_at=scheduled_at,
                    end_at=end_at,
                    frequency_per_day=1,
                    unit_price_override_cents=channel_quote["unit_cents"],
                    campaign_name="Bot 自助插播广告",
                    **creative_args,
                )
                if self.settings.bot_auto_approve_orders:
                    order = self.orders.approve_order(order["id"])
                orders.append(order)
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
        review_text = "✅ 已自动通过审核\n🚀 已进入发布队列" if self.settings.bot_auto_approve_orders else "⏳ 等待审核"
        if len(orders) > 1:
            channel_names = "、".join(self._short_title(str(channel["title"]), 8) for channel in channels[:3])
            if len(channels) > 3:
                channel_names += f" 等 {len(channels)} 个"
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=(
                    "✅ 批量投放已提交\n\n"
                    f"订单：{len(orders)} 个\n"
                    f"频道：{channel_names}\n"
                    f"展示：{self._placement_display_label(payload)}\n"
                    f"发布：{self._placement_period_label(payload)}\n"
                    f"冻结：USD {cents_to_money(quote['total_cents'])}\n\n"
                    f"{review_text}"
                ),
                inline_keyboard=[
                    [{"text": "📋 查看订单", "callback_data": "advertiser:orders"}],
                    [{"text": "🏠 工作台", "callback_data": "menu:home"}],
                ],
            )
            return {"handled": True, "type": "callback_placement_batch_created", "order_ids": [order["id"] for order in orders]}
        order = orders[0]
        channel = channels[0]
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

    def _placement_text(self, channel: dict[str, Any], payload: dict[str, Any], panel: str, account: dict[str, Any] | None = None) -> str:
        panel = self._placement_effective_panel(panel, payload)
        status_block = self._html_pre(
            [
                f"广告素材：{self._placement_creative_label(payload)}",
                f"频道：{self._placement_channel_summary_label(channel, payload)}",
                f"位置：{self._placement_display_label(payload)}",
                f"节奏：{self._placement_period_label(payload)}",
                f"时间：{payload.get('scheduled_label') or '立即发布'}",
                f"预算：{self._placement_cost_label(payload)}",
            ]
        )
        lines = [
            f"<b>🎯 给「{self._h(self._placement_target_label(channel, payload))}」投放广告</b>",
        ]
        quality_lines = self._channel_quality_lines(channel["id"])
        if quality_lines:
            lines.extend(["", self._html_quote("\n".join(quality_lines))])
        lines.extend(
            [
                "",
                status_block,
                "",
                f"<b>下一步：{self._h(self._placement_next_step(payload, account, panel))}</b>",
                "",
                f"<b>{self._h(self._placement_step_title(panel, payload))}</b>",
            ]
        )
        if panel == "creative":
            if payload.get("creative_text") and payload.get("target_url"):
                lines.append(self._html_quote("已选广告。可以直接下一步，也可以换一条广告。"))
            else:
                lines.append(self._html_quote("先选择一条已有广告；如果还没有，就添加一条新广告。"))
        elif panel == "display":
            lines.append(self._html_quote("选择广告在频道里的插播位置。这里只能选一种。"))
        elif panel == "schedule":
            lines.append(self._html_quote("设置发布周期。按广告生效开始计算，默认 24 小时发布一次。\n顶部预算会随发布周期和置顶选择自动变化。"))
        elif panel == "confirm":
            missing_panel = self._placement_missing_panel(payload)
            if missing_panel:
                lines.append(self._html_quote("还差一步配置，完成后再确认预算。"))
            else:
                quote = self._placement_quote(payload)
                available = int((account or {}).get("available_balance_cents") or 0)
                shortage = max(0, quote["total_cents"] - available)
                lines.append(
                    self._html_pre(
                        [
                            f"需要冻结：USD {cents_to_money(quote['total_cents'])}",
                            f"广告钱包可用：USD {cents_to_money(available)}",
                        ]
                    )
                )
                breakdown = [line for line in self._placement_cost_breakdown(channel, payload) if line]
                if breakdown:
                    lines.extend(["", self._html_quote("\n".join(breakdown))])
                if shortage:
                    lines.extend(["", self._html_quote(f"⚠️ 余额不足，还需充值 USD {cents_to_money(shortage)}。")])
                else:
                    lines.extend(["", self._html_quote("点击保存后广告生效；发布成功才扣费。")])
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

    def _placement_keyboard(
        self,
        panel: str,
        payload: dict[str, Any],
        creatives: list[Any] | None = None,
        account: dict[str, Any] | None = None,
    ) -> list[list[dict[str, str]]]:
        panel = self._placement_effective_panel(panel, payload)
        if panel == "display":
            selected = self.channels.normalize_slot_type(payload.get("slot_type") or "")
            keyboard: list[list[dict[str, str]]] = [
                [
                    {"text": self._placement_slot_button_text("button_tail", selected), "callback_data": "place:slot:button_tail"},
                    {"text": self._placement_slot_button_text("light_tail", selected), "callback_data": "place:slot:light_tail"},
                ],
                [
                    {"text": self._placement_slot_button_text("standard_card", selected), "callback_data": "place:slot:standard_card"},
                    {"text": self._placement_slot_button_text("strong_post", selected), "callback_data": "place:slot:strong_post"},
                ],
            ]
            keyboard.append(self._placement_nav_row(back=True, next_enabled=bool(selected)))
            return keyboard

        if panel == "schedule":
            current = payload.get("period") or "once"
            period_buttons = [
                {"text": f"{'✅ ' if current == key else ''}{value['label']}", "callback_data": f"place:period:{key}"}
                for key, value in PLACEMENT_PERIODS.items()
            ]
            keyboard = self._button_grid(period_buttons, 2)
            if self.channels.normalize_slot_type(payload.get("slot_type") or "") in PINNABLE_PLACEMENT_SLOTS:
                keyboard.append([{"text": "📌 是否置顶：是" if payload.get("pin") else "📌 是否置顶：否", "callback_data": "place:pin"}])
            keyboard.append([{"text": "🕒 发布时间", "callback_data": "place:time"}])
            keyboard.append(self._placement_nav_row(back=True, next_enabled=True))
            return keyboard

        if panel == "creative":
            keyboard: list[list[dict[str, str]]] = []
            creatives = creatives or []
            if creatives:
                for index, creative in enumerate(creatives):
                    label_source = creative.get("campaign_name") or creative.get("light_short_text") or creative.get("short_text") or creative.get("text") or ""
                    label = self._short_title(str(label_source).replace("\n", " "), 18)
                    marker = "✅" if creative.get("id") == payload.get("material_id") else "📄"
                    keyboard.append(
                        [
                            {"text": f"{marker} {label}", "callback_data": f"place:pick:{index}"},
                            {"text": "🗑 归档", "callback_data": f"place:archive:{index}"},
                        ]
                    )
                keyboard.append([{"text": "➕ 添加新广告", "callback_data": "place:new:auto"}])
            else:
                keyboard.append([{"text": "➕ 添加广告", "callback_data": "place:new:auto"}])
            if payload.get("creative_text") and payload.get("target_url"):
                keyboard.append([{"text": "下一步 ➡️", "callback_data": "place:next"}])
            keyboard.append([{"text": "🏠 工作台", "callback_data": "menu:home"}])
            return keyboard

        if panel == "confirm":
            missing_panel = self._placement_missing_panel(payload)
            if missing_panel:
                target_text = {"display": "📍 选择位置", "creative": "📁 选择广告"}.get(missing_panel, "继续配置")
                return [[{"text": target_text, "callback_data": f"place:{missing_panel}"}], [{"text": "⬅️ 上一步", "callback_data": "place:back"}]]
            quote = self._placement_quote(payload)
            available = int((account or {}).get("available_balance_cents") or 0)
            if available < quote["total_cents"]:
                return [
                    [{"text": "💰 充值", "callback_data": "advertiser:balance"}, {"text": "📍 降低配置", "callback_data": "place:display"}],
                    [{"text": "⬅️ 上一步", "callback_data": "place:back"}],
                ]
            return [
                [{"text": "✅ 保存并生效", "callback_data": "place:submit"}],
                [{"text": "⬅️ 上一步", "callback_data": "place:back"}],
            ]

        return [[{"text": "📁 选择广告", "callback_data": "place:creative"}], [{"text": "🏠 工作台", "callback_data": "menu:home"}]]

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

    def _placement_effective_panel(self, panel: str, payload: dict[str, Any]) -> str:
        if panel not in {"creative", "display", "schedule", "confirm"}:
            return "creative"
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        if panel == "schedule" and not slot_type:
            return "display"
        if panel == "schedule" and slot_type in CHANNEL_PACED_PLACEMENT_SLOTS:
            return "confirm"
        return panel

    def _placement_next_panel(self, payload: dict[str, Any], current_panel: str) -> str:
        current_panel = self._placement_effective_panel(current_panel, payload)
        if not payload.get("creative_text") or not payload.get("target_url"):
            return "creative"
        if current_panel == "creative":
            return "display"
        if not payload.get("slot_type"):
            return "display"
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        if current_panel == "display":
            return "schedule" if slot_type in SCHEDULED_PLACEMENT_SLOTS else "confirm"
        if current_panel == "schedule":
            return "confirm"
        return "confirm"

    def _placement_previous_panel(self, payload: dict[str, Any], current_panel: str) -> str:
        current_panel = self._placement_effective_panel(current_panel, payload)
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        if payload.get("launch_mode") == "global":
            if current_panel == "confirm":
                return "schedule" if slot_type in SCHEDULED_PLACEMENT_SLOTS else "channel"
            if current_panel == "schedule":
                return "channel"
        if current_panel == "confirm":
            return "schedule" if slot_type in SCHEDULED_PLACEMENT_SLOTS else "display"
        if current_panel == "schedule":
            return "display"
        if current_panel == "display":
            return "creative"
        return "creative"

    def _placement_step_title(self, panel: str, payload: dict[str, Any] | None = None) -> str:
        if (payload or {}).get("launch_mode") == "global":
            titles = {
                "creative": "第 1/5 步：选择广告素材",
                "display": "第 2/5 步：选择插播位置",
                "schedule": "第 4/5 步：配置发布节奏",
                "confirm": "第 5/5 步：确认预算",
            }
            return titles.get(panel, titles["creative"])
        titles = {
            "creative": "第 1/4 步：选择广告",
            "display": "第 2/4 步：选择插播位置",
            "schedule": "第 3/4 步：配置发布节奏",
            "confirm": "第 4/4 步：确认预算",
        }
        return titles.get(panel, titles["creative"])

    def _placement_nav_row(self, *, back: bool, next_enabled: bool) -> list[dict[str, str]]:
        row: list[dict[str, str]] = []
        if back:
            row.append({"text": "⬅️ 上一步", "callback_data": "place:back"})
        if next_enabled:
            row.append({"text": "下一步 ➡️", "callback_data": "place:next"})
        return row

    def _placement_slot_button_text(self, slot_type: str, selected: str) -> str:
        prefix = "✅ " if selected == slot_type else ""
        return f"{prefix}{self._slot_label(slot_type)}"

    def _placement_missing_panel(self, payload: dict[str, Any]) -> str | None:
        if not payload.get("creative_text") or not payload.get("target_url"):
            return "creative"
        if not payload.get("slot_type"):
            return "display"
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
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        if not slot_type:
            return "待选择"
        if slot_type in CHANNEL_PACED_PLACEMENT_SLOTS:
            return "随频道节奏"
        period = PLACEMENT_PERIODS.get(payload.get("period") or "once", PLACEMENT_PERIODS["once"])
        return str(period["label"])

    def _placement_creative_label(self, payload: dict[str, Any]) -> str:
        text = payload.get("creative_name") or payload.get("light_short_text") or payload.get("creative_text")
        return self._short_title(text.replace("\n", " "), 18) if text else "未选择"

    def _placement_channel_ids(self, payload: dict[str, Any]) -> list[str]:
        selected_ids = [str(channel_id) for channel_id in payload.get("selected_channel_ids") or [] if channel_id]
        if selected_ids:
            return list(dict.fromkeys(selected_ids))
        channel_id = payload.get("channel_id")
        return [str(channel_id)] if channel_id else []

    def _placement_target_label(self, channel: dict[str, Any], payload: dict[str, Any]) -> str:
        collection_label = self._placement_collection_label(payload)
        if collection_label:
            return collection_label
        channel_ids = self._placement_channel_ids(payload)
        if len(channel_ids) > 1:
            return f"{len(channel_ids)} 个频道"
        return str(channel["title"])

    def _placement_channel_summary_label(self, channel: dict[str, Any], payload: dict[str, Any]) -> str:
        collection_label = self._placement_collection_label(payload)
        if collection_label:
            return collection_label
        channel_ids = self._placement_channel_ids(payload)
        if len(channel_ids) > 1:
            return f"已选 {len(channel_ids)} 个（首个：{self._short_title(str(channel['title']), 12)}）"
        return str(channel["title"])

    def _placement_collection_label(self, payload: dict[str, Any]) -> str:
        if payload.get("selected_collection_names") or payload.get("selected_collection_name"):
            return self._global_channel_label(payload)
        return ""

    def _placement_cost_label(self, payload: dict[str, Any]) -> str:
        if not payload.get("slot_type"):
            return "待计算"
        quote = self._placement_quote(payload)
        return f"预计 USD {cents_to_money(quote['total_cents'])}"

    def _placement_next_step(self, payload: dict[str, Any], account: dict[str, Any] | None = None, panel: str = "creative") -> str:
        if not payload.get("creative_text") or not payload.get("target_url"):
            return "选择或添加广告"
        if not payload.get("slot_type"):
            return "选择插播位置"
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        if panel == "display" and slot_type in SCHEDULED_PLACEMENT_SLOTS:
            return "配置发布节奏"
        if panel in {"display", "schedule"}:
            return "确认预算"
        if account is not None:
            quote = self._placement_quote(payload)
            available = int(account.get("available_balance_cents") or 0)
            if available < quote["total_cents"]:
                return "充值或降低配置"
        return "保存并生效"

    def _placement_quote(self, payload: dict[str, Any]) -> dict[str, int]:
        channel_ids = self._placement_channel_ids(payload)
        if not channel_ids:
            raise ChaboError("请选择投放频道")
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        period_key = "once" if slot_type in CHANNEL_PACED_PLACEMENT_SLOTS else payload.get("period") or "once"
        period = PLACEMENT_PERIODS.get(period_key, PLACEMENT_PERIODS["once"])
        deliveries = int(period["deliveries"])
        channel_quotes = [self._placement_quote_for_channel(channel_id, payload) for channel_id in channel_ids]
        base_unit = sum(channel_quote["base_unit_cents"] for channel_quote in channel_quotes)
        unit_cents = sum(channel_quote["unit_cents"] for channel_quote in channel_quotes)
        return {
            "base_unit_cents": base_unit,
            "unit_cents": unit_cents,
            "total_cents": unit_cents * deliveries,
            "deliveries": deliveries,
            "channel_count": len(channel_ids),
        }

    def _placement_quote_for_channel(self, channel_id: str, payload: dict[str, Any]) -> dict[str, int]:
        base_unit = self._slot_price_cents(channel_id, payload["slot_type"])
        if payload.get("pin") and self.channels.normalize_slot_type(payload["slot_type"]) in PINNABLE_PLACEMENT_SLOTS:
            base_unit *= 2
        slot_type = self.channels.normalize_slot_type(payload.get("slot_type") or "")
        period_key = "once" if slot_type in CHANNEL_PACED_PLACEMENT_SLOTS else payload.get("period") or "once"
        period = PLACEMENT_PERIODS.get(period_key, PLACEMENT_PERIODS["once"])
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
                text = (creative["campaign_name"] or creative["text"] or "").replace("\n", " ")
                if len(text) > 28:
                    text = text[:28] + "..."
                short = creative["short_text"] or creative["button_text"]
                lines.append(f"{index}. {text}")
                lines.append(f"   {self._creative_status_label(creative['status'])} · {short} · {creative['button_text']}")
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
            if len(channels) > 10:
                lines.append("先显示前 10 个。")
            channel_buttons = [
                {"text": f"📺 {channel['title']}", "callback_data": f"pub:channel:{channel['ref_token']}"}
                for channel in channels[:10]
            ]
            keyboard = self._button_grid(channel_buttons, 2)
            keyboard.append([{"text": "🏠 返回主菜单", "callback_data": "menu:home"}])
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
                [{"text": "🏠 返回主菜单", "callback_data": "menu:home"}],
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

    def _send_channel_template_picker(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        target_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            target = self._sync_channel_profile_conn(conn, self._find_channel(conn, target_identifier))
        if not self._user_can_manage_channel(user_id, target):
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 未确认你是该频道管理员。",
                inline_keyboard=[[{"text": "⚙️ 频道设置", "callback_data": f"pub:channel:{target['ref_token']}"}]],
            )
            return

        candidates = [
            channel
            for channel in self._publisher_channels_for_user(user_id, self._display_name(user))
            if channel["id"] != target["id"]
        ]
        lines = [
            "📋 使用频道模版",
            "",
            f"目标频道：{target['title']}",
            "选择一个已配置频道，把广告形态、价格和基础设置复制过来。",
        ]
        keyboard: list[list[dict[str, str]]]
        if candidates:
            template_buttons = [
                {
                    "text": f"📋 {self._short_title(channel['title'], 13)}",
                    "callback_data": f"pub:tplapply:{target['ref_token']}:{channel['ref_token']}",
                }
                for channel in candidates[:10]
            ]
            keyboard = self._button_grid(template_buttons, 2)
            if len(candidates) > 10:
                lines.append("先显示前 10 个可用模版。")
        else:
            lines.extend(["", "还没有其他频道可作为模版。先完成一个频道的设置，再回来套用。"])
            keyboard = []
        keyboard.append([{"text": "⚙️ 频道设置", "callback_data": f"pub:channel:{target['ref_token']}"}])
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=keyboard,
        )

    def _apply_channel_template(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        target_identifier: str,
        source_identifier: str,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            target = self._find_channel(conn, target_identifier)
            source = self._find_channel(conn, source_identifier)
        if not self._user_can_manage_channel(user_id, target) or not self._user_can_manage_channel(user_id, source):
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text="⚠️ 只有两个频道的管理员才能套用模版。",
                inline_keyboard=[[{"text": "⚙️ 频道设置", "callback_data": f"pub:channel:{target['ref_token']}"}]],
            )
            return

        with self.db.transaction() as conn:
            self._copy_channel_template_conn(conn, source["id"], target["id"], actor_user_id=str(user_id))

        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "✅ 模版已应用\n\n"
                f"已把「{source['title']}」的广告形态、价格和基础设置复制到「{target['title']}」。"
            ),
            inline_keyboard=[
                [{"text": "⚙️ 频道设置", "callback_data": f"pub:channel:{target['ref_token']}"}],
                [{"text": "📋 换个模版", "callback_data": f"pub:template:{target['ref_token']}"}],
            ],
        )

    def _copy_channel_template_conn(
        self,
        conn: Any,
        source_channel_id: str,
        target_channel_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        source_config = conn.execute(
            "SELECT * FROM channel_configs WHERE channel_id = ?",
            (source_channel_id,),
        ).fetchone()
        if source_config:
            conn.execute(
                """
                INSERT INTO channel_configs (
                    channel_id, timezone, daily_ad_limit, allowed_start_hour, allowed_end_hour,
                    allow_pin, category_blocklist_json, service_fee_bps, holdback_bps, holdback_days
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    timezone = excluded.timezone,
                    daily_ad_limit = excluded.daily_ad_limit,
                    allowed_start_hour = excluded.allowed_start_hour,
                    allowed_end_hour = excluded.allowed_end_hour,
                    allow_pin = excluded.allow_pin,
                    category_blocklist_json = excluded.category_blocklist_json,
                    service_fee_bps = excluded.service_fee_bps,
                    holdback_bps = excluded.holdback_bps,
                    holdback_days = excluded.holdback_days,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    target_channel_id,
                    source_config["timezone"],
                    source_config["daily_ad_limit"],
                    source_config["allowed_start_hour"],
                    source_config["allowed_end_hour"],
                    source_config["allow_pin"],
                    source_config["category_blocklist_json"],
                    source_config["service_fee_bps"],
                    source_config["holdback_bps"],
                    source_config["holdback_days"],
                ),
            )

        policies = conn.execute(
            "SELECT * FROM channel_ad_format_policies WHERE channel_id = ?",
            (source_channel_id,),
        ).fetchall()
        for policy in policies:
            conn.execute(
                """
                INSERT INTO channel_ad_format_policies (
                    id, channel_id, format_type, enabled, owner_price_band,
                    platform_promo_enabled, custom_multiplier_bps
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, format_type) DO UPDATE SET
                    enabled = excluded.enabled,
                    owner_price_band = excluded.owner_price_band,
                    platform_promo_enabled = excluded.platform_promo_enabled,
                    custom_multiplier_bps = excluded.custom_multiplier_bps,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    new_id("pol"),
                    target_channel_id,
                    policy["format_type"],
                    policy["enabled"],
                    policy["owner_price_band"],
                    policy["platform_promo_enabled"],
                    policy["custom_multiplier_bps"],
                ),
            )

        slots = conn.execute(
            """
            SELECT s.slot_type, s.enabled, s.min_days,
                   r.currency, r.unit_price_cents, r.pricing_unit
            FROM ad_slots s
            LEFT JOIN rate_cards r ON r.slot_id = s.id AND r.active = 1
            WHERE s.channel_id = ?
            ORDER BY s.slot_type
            """,
            (source_channel_id,),
        ).fetchall()
        for slot in slots:
            conn.execute(
                """
                INSERT INTO ad_slots (id, channel_id, slot_type, enabled, min_days)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, slot_type) DO UPDATE SET
                    enabled = excluded.enabled,
                    min_days = excluded.min_days
                """,
                (new_id("slot"), target_channel_id, slot["slot_type"], slot["enabled"], slot["min_days"]),
            )
            target_slot = conn.execute(
                "SELECT * FROM ad_slots WHERE channel_id = ? AND slot_type = ?",
                (target_channel_id, slot["slot_type"]),
            ).fetchone()
            if slot["unit_price_cents"] is None:
                continue
            conn.execute("UPDATE rate_cards SET active = 0 WHERE slot_id = ?", (target_slot["id"],))
            conn.execute(
                """
                INSERT INTO rate_cards (id, slot_id, currency, unit_price_cents, pricing_unit, active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    new_id("rate"),
                    target_slot["id"],
                    slot["currency"],
                    slot["unit_price_cents"],
                    slot["pricing_unit"],
                ),
            )

        insert_audit_log(
            conn,
            actor_account_id=None,
            action="channel_template_applied",
            entity_type="channel",
            entity_id=target_channel_id,
            payload={
                "source_channel_id": source_channel_id,
                "target_channel_id": target_channel_id,
                "actor_telegram_user_id": actor_user_id,
            },
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
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
        summary = self.ledger.get_earnings_summary(telegram_user_id=user_id)
        notification_button = (
            {"text": "🔕 关闭通知", "callback_data": "publisher:disable_income_notifications"}
            if bool(account["publisher_income_notifications_enabled"])
            else {"text": "🔔 开启通知", "callback_data": "publisher:enable_income_notifications"}
        )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "💰 我的钱包\n"
                "💸 我的收益\n\n"
                f"⏳ 待确认：USD {cents_to_money(summary['pending_earnings_cents'])}\n"
                f"✅ 已确认：USD {cents_to_money(summary['confirmed_earnings_cents'])}\n"
                f"💵 可结算：USD {cents_to_money(summary['releasable_earnings_cents'])}"
            ),
            inline_keyboard=[
                [{"text": "📊 频道分布", "callback_data": "earnings:channels"}, {"text": "📜 收益流水", "callback_data": "earnings:statement"}],
                [notification_button, {"text": "📺 频道管理", "callback_data": "publisher:channels"}],
                [{"text": "🏠 主菜单", "callback_data": "menu:home"}],
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
        if state["flow"] == "channel_market":
            return self._handle_channel_market_message(message, state, text)
        if state["flow"] == "global_placement":
            return self._handle_global_placement_message(message, state, text)
        if state["flow"] == "placement_config":
            return self._handle_placement_message(message, state, text)
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
            review_text = "✅ 已自动通过审核\n🚀 已进入发布队列" if self.settings.bot_auto_approve_orders else "⏳ 等待审核"
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

    def _handle_channel_market_message(self, message: dict[str, Any], state: dict[str, Any], text: str) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        user_id = user.get("id") or chat_id
        if not chat_id:
            return {"handled": False, "reason": "missing_chat"}
        if state["step"] != "folder_name":
            return {"handled": False, "reason": "unsupported_channel_market_step"}
        clean_name = self._clean_collection_name(text)
        previous_payload = json.loads(state["payload_json"] or "{}")
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            collection = self._ensure_channel_collection_conn(conn, account["id"], clean_name)
            total = self._market_channel_count_conn(conn)
            offset = int(previous_payload.get("offset") or 0)
            if total and offset >= total:
                offset = max(0, total - 1)
            payload = {
                "collection_id": collection["id"],
                "collection_name": collection["name"],
                "offset": offset,
            }
            if previous_payload.get("channel_id"):
                payload["channel_id"] = previous_payload["channel_id"]
            self._set_conversation_conn(conn, chat_id, account["id"], "channel_market", "browse", payload)
        self._send_channel_market_browse(chat_id, user, None, payload=payload)
        return {"handled": True, "type": "channel_market_folder_created", "collection_id": collection["id"]}

    def _handle_global_placement_message(self, message: dict[str, Any], state: dict[str, Any], text: str) -> dict[str, Any]:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id") or user.get("id")
        user_id = user.get("id") or chat_id
        if not chat_id:
            return {"handled": False, "reason": "missing_chat"}
        if state["step"] != "folder_name":
            return {"handled": False, "reason": "unsupported_global_placement_step"}
        clean_name = self._clean_collection_name(text)
        previous_payload = json.loads(state["payload_json"] or "{}")
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            collection = self._ensure_channel_collection_conn(conn, account["id"], clean_name)
            payload = {
                "collection_id": collection["id"],
                "collection_name": collection["name"],
                "offset": 0,
            }
            if previous_payload.get("channel_id"):
                payload["channel_id"] = previous_payload["channel_id"]
        self._send_channel_market_browse(chat_id, user, None, payload=payload)
        return {"handled": True, "type": "global_placement_folder_created", "collection_id": collection["id"]}

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

        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join(lines),
            inline_keyboard=[
                [{"text": "📋 投放订单", "callback_data": "advertiser:orders"}],
                [{"text": "💰 广告钱包", "callback_data": "advertiser:balance"}, {"text": "🏠 主菜单", "callback_data": "menu:home"}],
            ],
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

    def _send_channel_quote(
        self,
        chat_id: str | int,
        channel_id: str,
        user: dict[str, Any] | None = None,
        source_message: dict[str, Any] | None = None,
    ) -> None:
        user = user or {}
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
        if self._user_can_manage_channel(user.get("id") or chat_id, channel):
            keyboard = [
                [{"text": "⚙️ 广告形态", "callback_data": f"pub:formats:{channel['ref_token']}"}],
                [{"text": "⬅️ 频道详情", "callback_data": f"pub:channel:{channel['ref_token']}"}, {"text": "🏠 工作台", "callback_data": "menu:home"}],
            ]
        else:
            keyboard = [self._channel_keyboard(channel_id)[0], [{"text": "🏠 工作台", "callback_data": "menu:home"}]]
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text="\n".join([f"💵 {channel['title']}", *self._rate_lines(rates)]),
            inline_keyboard=keyboard,
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

    def _account_active_role(self, account: dict[str, Any]) -> str | None:
        role = account.get("active_role")
        return role if role in {"publisher", "advertiser"} else None

    def _prompt_role(
        self,
        chat_id: str | int,
        account_id: str,
        pending_start_payload: str,
        source_message: dict[str, Any] | None,
        *,
        conn: Any | None = None,
        switch: bool = False,
    ) -> None:
        payload = {"pending_start_payload": pending_start_payload}
        if conn is not None:
            self._set_conversation_conn(conn, chat_id, account_id, "role_setup", "choose", payload)
        else:
            self._set_conversation(chat_id, account_id, "role_setup", "choose", payload)
        title = "🔁 切换身份" if switch else "👋 先选择身份"
        hint = (
            "切换后，首页只展示对应身份的工作台。"
            if switch
            else "之后首页会默认进入你选择的工作台；需要更换时，可在设置里切换。"
        )
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                f"{title}\n\n"
                "你现在主要想做哪件事？\n\n"
                "📺 频道主：接入频道、设置广告位、查看收益。\n"
                "📣 广告主：创建素材、挑选频道、投放广告。\n\n"
                f"{hint}"
            ),
            inline_keyboard=[
                [{"text": "📺 我是频道主", "callback_data": "role:publisher"}],
                [{"text": "📣 我是广告主", "callback_data": "role:advertiser"}],
            ],
        )

    def _set_active_role(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        active_role: str,
        source_message: dict[str, Any] | None,
    ) -> None:
        if active_role not in {"publisher", "advertiser"}:
            raise NotFound(f"unsupported role: {active_role}")
        account = self._ensure_mixed_account(user, chat_id)
        state = self._get_conversation(chat_id)
        payload = json.loads(state["payload_json"] or "{}") if state and state["flow"] == "role_setup" else {}
        pending_start_payload = payload.get("pending_start_payload") or ""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET active_role = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (active_role, account["id"]),
            )
            conn.execute("DELETE FROM bot_conversation_states WHERE chat_id = ?", (str(chat_id),))
        if active_role == "advertiser" and pending_start_payload and pending_start_payload not in {"role", "switch", "settings"}:
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
        if pending_start_payload and pending_start_payload not in {"role", "switch", "settings"}:
            self._handle_start(
                {
                    "from": user,
                    "chat": {"id": chat_id},
                    "text": f"/start {pending_start_payload}",
                },
                pending_start_payload,
            )
            return
        account = self._ensure_mixed_account(user, chat_id)
        active_role = self._account_active_role(account)
        if not active_role:
            self._prompt_role(chat_id, account["id"], pending_start_payload, source_message)
            return
        if pending_start_payload in {"role", "switch", "settings"}:
            self._prompt_role(chat_id, account["id"], "", source_message, switch=True)
            return
        self._send_main_menu(chat_id, source_message, user)

    def _resolve_timezone(self, raw_value: str) -> str | None:
        return resolve_timezone(raw_value)

    def _timezone_now(self, timezone_name: str) -> str:
        return format_timezone_now(timezone_name)

    def _add_channel_url(self) -> str:
        return f"https://t.me/{self.settings.bot_username}?startchannel&admin=post_messages+edit_messages+pin_messages"

    def _send_web_magic_link(
        self,
        chat_id: str | int,
        user: dict[str, Any],
        source_message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.public_base_url:
            self._reply_or_edit(
                chat_id=chat_id,
                source_message=source_message,
                text=(
                    "🌐 网页端入口还没配置\n\n"
                    "请先在服务端设置 CHABO_PUBLIC_BASE_URL，例如 https://chabo.example。"
                ),
                inline_keyboard=[[{"text": "🏠 返回工作台", "callback_data": "menu:home"}]],
            )
            return {"handled": True, "type": "callback_web_link_unconfigured"}
        user_id = user.get("id") or chat_id
        with self.db.transaction() as conn:
            account = self.accounts.get_or_create_by_telegram(conn, user_id, "mixed", self._display_name(user))
            activation = sync_portal_access_from_activity(conn, account["id"], ensure_login_candidates=True)
            token, row = issue_login_token_conn(
                conn,
                account_id=account["id"],
                ttl_seconds=self.settings.magic_link_ttl_seconds,
            )
        url = build_magic_link_url(self.settings, token)
        self._reply_or_edit(
            chat_id=chat_id,
            source_message=source_message,
            text=(
                "🌐 网页端已准备好\n\n"
                "这个链接只能使用一次，过期后请回到 Bot 重新生成。\n"
                "如果你的广告主端或频道主端还在待开通，网页端会显示对应状态。"
            ),
            inline_keyboard=[
                [{"text": "打开网页端", "url": url}],
                [{"text": "🏠 返回工作台", "callback_data": "menu:home"}],
            ],
        )
        return {
            "handled": True,
            "type": "callback_web_magic_link",
            "account_id": account["id"],
            "expires_at": row["expires_at"],
            "activation": activation,
        }

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
                insert_audit_log(
                    conn,
                    actor_account_id=None,
                    action="sync_channel_admins_failed",
                    entity_type="channel",
                    entity_id=channel_id,
                    payload={"error": str(exc)},
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
                        [
                            {"text": "⚙️ 频道设置", "callback_data": f"pub:channel:{channel['ref_token']}"},
                            {"text": "📋 使用模版", "callback_data": f"pub:template:{channel['ref_token']}"},
                        ],
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
                    inline_keyboard=[
                        [
                            {"text": "⚙️ 频道设置", "callback_data": f"pub:channel:{channel['ref_token']}"},
                            {"text": "📋 使用模版", "callback_data": f"pub:template:{channel['ref_token']}"},
                        ],
                    ],
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
                  AND p.format_type IN ('light_tail', 'button_tail', 'standard_card', 'strong_post')
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
        lines = [
            f"⚙️ {channel['title']}",
            "",
            "点击开关频道愿意接的广告形态。",
            "置顶和循环发布由广告主在投放设置里选择。",
        ]
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
            [{"text": "📣 投放这个频道", "callback_data": f"channel:order:{channel_id}"}],
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
        return [
            f"• {self._slot_label(rate['slot_type'])}：{rate['currency']} {rate['unit_price_cents'] / 100:.2f}"
            for rate in rates
            if self.channels.normalize_slot_type(rate["slot_type"]) in PLACEMENT_SLOT_TYPES
        ]

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

    def _h(self, value: Any) -> str:
        return html.escape(str(value), quote=True)

    def _html_pre(self, lines: list[str]) -> str:
        return "<pre>" + self._h("\n".join(lines)) + "</pre>"

    def _html_quote(self, text: str) -> str:
        return "<blockquote>" + self._h(text) + "</blockquote>"

    def _reply_or_edit(
        self,
        *,
        chat_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        source_message: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> str | None:
        message_id = source_message.get("message_id") if source_message else None
        if message_id:
            try:
                self.gateway.edit_private_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    inline_keyboard=inline_keyboard,
                    parse_mode=parse_mode,
                )
                return str(message_id)
            except TelegramError:
                pass
        return self.gateway.send_private_message(chat_id=chat_id, text=text, inline_keyboard=inline_keyboard, parse_mode=parse_mode)

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
