from __future__ import annotations

import json
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chabo.app import create_app
from chabo.bot import UpdateHandler
from chabo.config import Settings
from chabo.fulfillment import FulfillmentService
from chabo.money import money_to_cents
from chabo.polling import PollingRunner
from chabo.telegram import TelegramError
from chabo.web import make_server


class FakeGateway:
    def __init__(self) -> None:
        self.next_message_id = 100
        self.sent_ads = []
        self.sent_media_ads = []
        self.private_messages = []
        self.invoices = []
        self.pre_checkout_answers = []
        self.callback_answers = []
        self.edits = []
        self.text_edits = []
        self.channel_text_edits = []
        self.pins = []
        self.fail_send = False
        self.fail_pin = False
        self.bot_user = {"id": 999001, "username": "ChaBoTestBot"}
        self.chats = {}
        self.chat_members = {}
        self.chat_administrators = {}

    def send_ad(self, *, chat_id: str, text: str, button_text: str, button_url: str) -> str:
        if self.fail_send:
            raise TelegramError("send failed")
        self.next_message_id += 1
        message_id = str(self.next_message_id)
        self.sent_ads.append(
            {
                "chat_id": chat_id,
                "text": text,
                "button_text": button_text,
                "button_url": button_url,
                "message_id": message_id,
            }
        )
        return message_id

    def send_media_ad(
        self,
        *,
        chat_id: str,
        media_file_id: str,
        media_type: str,
        caption: str,
        button_text: str,
        button_url: str,
    ) -> str:
        if self.fail_send:
            raise TelegramError("send failed")
        self.next_message_id += 1
        message_id = str(self.next_message_id)
        self.sent_media_ads.append(
            {
                "chat_id": chat_id,
                "media_file_id": media_file_id,
                "media_type": media_type,
                "caption": caption,
                "button_text": button_text,
                "button_url": button_url,
                "message_id": message_id,
            }
        )
        return message_id

    def send_private_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        parse_mode: str | None = None,
    ) -> str | None:
        self.private_messages.append({"chat_id": str(chat_id), "text": text, "inline_keyboard": inline_keyboard, "parse_mode": parse_mode})
        return "pm_1"

    def send_invoice(
        self,
        *,
        chat_id: str | int,
        title: str,
        description: str,
        payload: str,
        prices: list[dict[str, int | str]],
        currency: str = "XTR",
    ) -> str | None:
        self.invoices.append(
            {
                "chat_id": str(chat_id),
                "title": title,
                "description": description,
                "payload": payload,
                "prices": prices,
                "currency": currency,
            }
        )
        return "inv_1"

    def answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> None:
        self.pre_checkout_answers.append(
            {
                "pre_checkout_query_id": pre_checkout_query_id,
                "ok": ok,
                "error_message": error_message,
            }
        )

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        self.callback_answers.append(
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            }
        )

    def get_me(self) -> dict:
        return self.bot_user

    def get_chat(self, *, chat_id: str | int) -> dict:
        key = str(chat_id)
        if key not in self.chats:
            raise TelegramError("chat not found")
        return self.chats[key]

    def get_chat_member(self, *, chat_id: str | int, user_id: str | int) -> dict:
        key = (str(chat_id), str(user_id))
        if key not in self.chat_members:
            raise TelegramError("member not found")
        return self.chat_members[key]

    def get_chat_administrators(self, *, chat_id: str | int) -> list[dict]:
        key = str(chat_id)
        if key not in self.chat_administrators:
            raise TelegramError("administrators not found")
        return self.chat_administrators[key]

    def edit_message_reply_markup(self, *, chat_id: str, message_id: str | int, inline_keyboard: list[list[dict[str, str]]]) -> None:
        self.edits.append({"chat_id": str(chat_id), "message_id": str(message_id), "inline_keyboard": inline_keyboard})

    def edit_private_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.text_edits.append({"chat_id": str(chat_id), "message_id": str(message_id), "text": text, "inline_keyboard": inline_keyboard, "parse_mode": parse_mode})

    def edit_channel_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.channel_text_edits.append({"chat_id": str(chat_id), "message_id": str(message_id), "text": text, "inline_keyboard": inline_keyboard})

    def pin_message(self, *, chat_id: str, message_id: str | int) -> None:
        if self.fail_pin:
            raise TelegramError("pin failed")
        self.pins.append({"chat_id": str(chat_id), "message_id": str(message_id)})


class ChaboMvpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = FakeGateway()
        self.settings = Settings(
            db_path=str(Path(self.tmp.name) / "test.sqlite3"),
            bot_username="ChaBoTestBot",
        )
        self.app = create_app(self.settings, self.gateway)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def bind_channel(self):
        return self.app.channels.bind_channel(
            telegram_chat_id=-100123,
            title="测试频道",
            username="test_channel",
            owner_telegram_user_id=20001,
            owner_display_name="频道主",
        )

    def topup_advertiser(self, amount: str = "20"):
        return self.app.ledger.manual_topup(10001, money_to_cents(amount), display_name="广告主")

    def confirm_timezone(self, telegram_user_id: int, role: str = "mixed", display_name: str = "测试用户") -> None:
        with self.app.db.transaction() as conn:
            account = self.app.update_handler.accounts.get_or_create_by_telegram(conn, telegram_user_id, role, display_name)
            conn.execute(
                """
                UPDATE accounts
                SET timezone = 'Asia/Shanghai', timezone_confirmed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (account["id"],),
            )

    def complete_placement_ad_asset(self, *, name: str = "测试广告资产", target_url: str = "https://asset.example") -> dict:
        user = {"id": 10001, "first_name": "广告主"}
        messages = [
            {"message_id": 8101, "text": name},
            {"message_id": 8102, "photo": [{"file_id": "photo_small"}, {"file_id": "photo_large"}]},
            {"message_id": 8103, "text": "这是通过引导流程创建的完整广告详情，适合定制插播和详情页展示。"},
            {"message_id": 8104, "text": target_url},
            {"message_id": 8105, "text": "立即了解"},
            {"message_id": 8106, "text": "限时福利"},
            {
                "message_id": 8107,
                "text": "这是标准插播文案\\n最多五行\\n用于频道里克制展示",
            },
        ]
        result = {"handled": False}
        for item in messages:
            message = {"from": user, "chat": {"id": 10001}, **item}
            result = self.app.update_handler.handle({"message": message})
        return result

    def create_approved_order(self, *, slot_type: str = "standard", budget: str = "10"):
        channel = self.bind_channel()
        self.topup_advertiser(budget)
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type=slot_type,
            text="这是一条插播广告",
            target_url="https://example.com",
            budget_cents=money_to_cents(budget),
        )
        return channel, self.app.orders.approve_order(order["id"])

    def start_http_server(self, *, admin_token: str = "admin-token", webhook_secret: str = "webhook-secret") -> str:
        server = make_server(
            app=self.app,
            host="127.0.0.1",
            port=0,
            admin_token=admin_token,
            webhook_secret=webhook_secret,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def http_json(
        self,
        method: str,
        url: str,
        payload: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_channel_button_preserves_existing_buttons_and_start_attribution(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")

        result = self.app.update_handler.handle(
            {
                "channel_post": {
                    "message_id": 7,
                    "chat": {"id": -100123, "title": "测试频道"},
                    "text": "新帖",
                    "reply_markup": {
                        "inline_keyboard": [[{"text": "原按钮", "url": "https://old.example"}]]
                    },
                }
            }
        )

        self.assertTrue(result["handled"])
        keyboard = self.gateway.edits[0]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "原按钮")
        self.assertEqual(keyboard[-1][0]["text"], "📣 频道招商")
        self.assertIn(channel["ref_token"], keyboard[-1][0]["url"])
        self.assertIn("start=ch_", keyboard[-1][0]["url"])

        start = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 1,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": f"/start ch_{channel['ref_token']}",
                }
            }
        )
        self.assertEqual(start["type"], "channel_start")
        self.assertEqual(start["channel_id"], channel["id"])
        landing = self.gateway.private_messages[-1]
        self.assertIn("给「测试频道」投放广告", landing["text"])
        self.assertIn("位置：未选择", landing["text"])
        self.assertIn("下一步：选择或添加广告", landing["text"])
        self.assertIn("第 1/4 步：选择广告", landing["text"])
        button_texts = [button["text"] for row in landing["inline_keyboard"] for button in row]
        self.assertIn("➕ 添加广告", button_texts)
        self.assertNotIn("🗂 广告库", button_texts)
        self.assertNotIn("💰 广告钱包", button_texts)

        with self.app.db.transaction() as conn:
            session = conn.execute("SELECT * FROM advertiser_sessions").fetchone()
        self.assertEqual(session["ref_channel_id"], channel["id"])

    def test_channel_sales_entry_uses_placement_configurator(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("30")
        with self.app.db.transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            conn.execute(
                """
                INSERT INTO campaigns (id, advertiser_account_id, name, status)
                VALUES ('camp_existing', ?, '已有广告', 'active')
                """,
                (account["id"],),
            )
            conn.execute(
                """
                INSERT INTO creatives (id, campaign_id, text, target_url, button_text, status, content_hash)
                VALUES ('cre_existing', 'camp_existing', '已有广告素材', 'https://existing.example', '查看详情', 'approved', 'hash_existing')
                """
            )

        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 1,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": f"/start ch_{channel['ref_token']}",
                }
            }
        )
        display = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_pick",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:pick:0",
                }
            }
        )
        display_message = self.gateway.private_messages[-1]
        display_buttons = [button["text"] for row in display_message["inline_keyboard"] for button in row]
        slot = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_slot",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:slot:standard_card",
                }
            }
        )
        schedule = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_schedule",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:next",
                }
            }
        )
        schedule_message = self.gateway.private_messages[-1]
        pinned = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_pin",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:pin",
                }
            }
        )
        weekly = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_week",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:period:week",
                }
            }
        )

        self.assertEqual(display["type"], "callback_placement_creative_selected")
        self.assertEqual(slot["type"], "callback_placement_slot")
        self.assertEqual(schedule["panel"], "schedule")
        self.assertEqual(pinned["type"], "callback_placement_pin")
        self.assertEqual(weekly["type"], "callback_placement_period")
        self.assertIn("第 2/4 步：选择插播位置", display_message["text"])
        self.assertEqual(display_buttons[:4], ["🔘 按钮插播", "✍️ 文字插播", "🧾 标准插播", "🎨 定制插播"])
        self.assertFalse(any("置顶" in button for button in display_buttons))
        self.assertIn("第 3/4 步：配置发布节奏", schedule_message["text"])
        self.assertIn("📌 是否置顶：否", [button["text"] for row in schedule_message["inline_keyboard"] for button in row])
        self.assertIn("位置：标准插播 + 置顶", self.gateway.private_messages[-1]["text"])
        self.assertIn("节奏：连续 1 周", self.gateway.private_messages[-1]["text"])
        self.assertIn("预算：预计 USD 126.00", self.gateway.private_messages[-1]["text"])

    def test_guided_placement_flow_selects_ad_then_position_and_skips_rhythm_for_button_tail(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("20")

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_guided_start",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"channel:order:{channel['id']}",
                }
            }
        )
        first_page = self.gateway.private_messages[-1]
        first_buttons = [button["text"] for row in first_page["inline_keyboard"] for button in row]

        self.assertIn("第 1/4 步：选择广告", first_page["text"])
        self.assertIn("➕ 添加广告", first_buttons)
        self.assertNotIn("下一步 ➡️", first_buttons)

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_guided_new",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:new:auto",
                }
            }
        )
        asset_prompt = self.gateway.private_messages[-1]
        self.assertIn("创建广告资产", asset_prompt["text"])
        self.assertIn("👉 当前填写：广告名称", asset_prompt["text"])
        self.assertIn("完成度：0/7", asset_prompt["text"])
        self.assertIn("广告名称", asset_prompt["text"])
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 61,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "引导式广告资产",
                }
            }
        )
        media_prompt = self.gateway.private_messages[-1]
        self.assertIn("第 2/7 步", media_prompt["text"])
        self.assertIn("👉 当前填写：媒体文件", media_prompt["text"])
        self.assertIn("✅ 广告名称：引导式广告资产", media_prompt["text"])
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 62,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "photo": [{"file_id": "photo_small"}, {"file_id": "photo_large"}],
                }
            }
        )
        detail_prompt = self.gateway.private_messages[-1]
        self.assertIn("第 3/7 步", detail_prompt["text"])
        self.assertIn("👉 当前填写：详细介绍", detail_prompt["text"])
        self.assertIn("✅ 媒体文件：图片已收到", detail_prompt["text"])
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 63,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "这是通过引导流程创建的完整广告详情，适合定制插播和详情页展示。",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 64,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "https://guided.example",
                }
            }
        )
        button_prompt = self.gateway.private_messages[-1]
        self.assertIn("第 5/7 步", button_prompt["text"])
        self.assertIn("👉 当前填写：按钮名称", button_prompt["text"])
        self.assertIn("按钮上显示的文字", button_prompt["text"])
        self.assertIn("✅ 跳转链接：https://guid...", button_prompt["text"])
        self.assertNotIn("立即了解", button_prompt["text"])
        self.assertIn("⬅️ 返回上一步", [button["text"] for row in button_prompt["inline_keyboard"] for button in row])
        back_to_link = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_guided_asset_back",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:asset_back",
                }
            }
        )
        link_prompt = self.gateway.private_messages[-1]
        self.assertEqual(back_to_link["type"], "callback_placement_asset_back")
        self.assertEqual(back_to_link["step"], "target_url")
        self.assertIn("👉 当前填写：跳转链接", link_prompt["text"])
        self.assertNotIn("✅ 跳转链接", link_prompt["text"])
        self.assertIn("✅ 详细介绍：", link_prompt["text"])
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 6401,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "https://guided.example/fixed",
                }
            }
        )
        for message in [
            {"message_id": 65, "text": "查看"},
            {"message_id": 66, "text": "限时福利"},
            {"message_id": 67, "text": "这是标准插播文案\\n最多五行\\n用于频道里克制展示"},
        ]:
            self.app.update_handler.handle(
                {
                    "message": {
                        "from": {"id": 10001, "first_name": "广告主"},
                        "chat": {"id": 10001},
                        **message,
                    }
                }
            )
        position_page = self.gateway.private_messages[-1]
        position_buttons = [button["text"] for row in position_page["inline_keyboard"] for button in row]

        self.assertIn("第 2/4 步：选择插播位置", position_page["text"])
        self.assertEqual(position_buttons[:4], ["🔘 按钮插播", "✍️ 文字插播", "🧾 标准插播", "🎨 定制插播"])
        self.assertFalse(any("置顶" in button for button in position_buttons))

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_guided_button_slot",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:slot:button_tail",
                }
            }
        )
        selected_position = self.gateway.private_messages[-1]
        self.assertIn("位置：按钮插播", selected_position["text"])
        self.assertIn("节奏：随频道节奏", selected_position["text"])

        next_page = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_guided_next",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:next",
                }
            }
        )

        self.assertEqual(next_page["panel"], "confirm")
        self.assertIn("第 4/4 步：确认预算", self.gateway.private_messages[-1]["text"])

    def test_placement_configurator_creates_order_without_manual_budget_step(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("20")

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_create_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"channel:order:{channel['id']}",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_create_2",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:slot:standard_card",
                }
            }
        )
        new_creative = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_create_3",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:new:auto",
                }
            }
        )
        url = self.complete_placement_ad_asset(name="标准插播测试资产", target_url="https://placement.example")
        submit = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_submit",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:submit",
                }
            }
        )

        with self.app.db.transaction() as conn:
            order = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (submit["order_id"],)).fetchone()
            creative_row = conn.execute("SELECT * FROM creatives WHERE id = ?", (order["creative_id"],)).fetchone()
            state = conn.execute("SELECT * FROM bot_conversation_states WHERE chat_id = '10001'").fetchone()

        self.assertEqual(new_creative["type"], "callback_placement_new_creative")
        self.assertEqual(url["type"], "placement_standard_text_saved")
        self.assertEqual(submit["type"], "callback_placement_order_created")
        self.assertEqual(order["status"], "approved")
        self.assertEqual(order["budget_cents"], 1000)
        self.assertEqual(order["unit_price_cents"], 1000)
        self.assertEqual(creative_row["target_url"], "https://placement.example")
        self.assertEqual(creative_row["short_text"], "限时福利")
        self.assertEqual(creative_row["button_text"], "立即了解")
        self.assertEqual(creative_row["media_file_id"], "photo_large")
        self.assertIsNone(state)
        self.app.fulfillment.dispatch_due()
        self.assertEqual(len(self.gateway.sent_media_ads), 1)
        media_ad = self.gateway.sent_media_ads[-1]
        self.assertEqual(media_ad["media_file_id"], "photo_large")
        self.assertEqual(media_ad["media_type"], "photo")
        self.assertIn("这是标准插播文案", media_ad["caption"])
        self.assertEqual(media_ad["button_text"], "立即了解")

    def test_placement_confirmation_blocks_when_balance_is_short(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_short_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"channel:order:{channel['id']}",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_short_2",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:slot:standard_card",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_short_3",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:new:auto",
                }
            }
        )
        self.complete_placement_ad_asset(name="余额不足测试资产", target_url="https://short-balance.example")
        confirm = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_short_confirm",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:confirm",
                }
            }
        )

        message = self.gateway.private_messages[-1]
        button_texts = [button["text"] for row in message["inline_keyboard"] for button in row]

        self.assertEqual(confirm["type"], "callback_placement_confirm")
        self.assertIn("下一步：充值或降低配置", message["text"])
        self.assertIn("广告钱包可用：USD 0.00", message["text"])
        self.assertIn("余额不足", message["text"])
        self.assertIn("💰 充值", button_texts)
        self.assertIn("📍 降低配置", button_texts)
        self.assertNotIn("✅ 保存并生效", button_texts)

    def test_channel_management_syncs_latest_title_from_telegram(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self.gateway.chats[str(-100123)] = {
            "id": -100123,
            "type": "channel",
            "title": "广告投放测试",
            "username": "ad_test",
        }

        result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_sync_title",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}},
                    "data": "publisher:channels",
                }
            }
        )

        self.assertEqual(result["type"], "callback_publisher_channels")
        keyboard = self.gateway.private_messages[-1]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "📺 广告投放测试")
        with self.app.db.transaction() as conn:
            refreshed = conn.execute("SELECT * FROM channels WHERE id = ?", (channel["id"],)).fetchone()
        self.assertEqual(refreshed["title"], "广告投放测试")
        self.assertEqual(refreshed["username"], "ad_test")

    def test_start_menu_and_callback_navigation(self) -> None:
        first_start = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 1,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "/start",
                }
            }
        )
        timezone_ok = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_tz_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 10},
                    "data": "timezone:yes",
                }
            }
        )
        start = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 2,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "/menu",
                }
            }
        )
        advertiser_menu = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "role:advertiser",
                }
            }
        )
        balance = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_2",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "advertiser:balance",
                }
            }
        )
        library = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_3",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "advertiser:library",
                }
            }
        )
        self.assertIn("广告库", self.gateway.private_messages[-1]["text"])
        settings = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_4",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "settings:home",
                }
            }
        )

        self.assertEqual(first_start["type"], "timezone_prompt")
        self.assertIn("时区确认", self.gateway.private_messages[0]["text"])
        self.assertEqual(timezone_ok["type"], "callback_timezone_confirmed")
        self.assertEqual(start["type"], "main_menu")
        main_keyboard = self.gateway.text_edits[-1]["inline_keyboard"]
        main_buttons = [button["text"] for row in main_keyboard for button in row]
        self.assertEqual(
            main_buttons,
            [
                "➕ 添加频道",
                "➕ 广告投放",
                "📺 频道管理",
                "🔎 频道广场",
                "📋 我的广告",
                "🗂 广告素材",
                "💸 我的收益",
                "⭐ 频道收藏夹",
                "💰 我的钱包",
                "⚙️ 设置",
            ],
        )
        self.assertIn("Telegram 频道广告协作工具", self.gateway.text_edits[-1]["text"])
        self.assertIn("让好频道获得透明收益", self.gateway.text_edits[-1]["text"])
        self.assertIn("频道主：添加频道", self.gateway.text_edits[-1]["text"])
        self.assertIn("广告主：创建素材", self.gateway.text_edits[-1]["text"])
        self.assertIn("发布成功才扣费", self.gateway.text_edits[-1]["text"])
        self.assertNotIn("我是广告主", self.gateway.text_edits[-1]["text"])
        self.assertNotIn("我是频道主", self.gateway.text_edits[-1]["text"])
        self.assertTrue(all(len(row) == 2 for row in main_keyboard))
        self.assertTrue(any(message["inline_keyboard"] and message["inline_keyboard"][0][0]["text"] == "➕ 添加频道" for message in self.gateway.private_messages))
        self.assertEqual(advertiser_menu["type"], "callback_advertiser_menu")
        self.assertEqual(balance["type"], "callback_advertiser_balance")
        self.assertEqual(library["type"], "callback_advertiser_library")
        self.assertEqual(settings["type"], "callback_settings_menu")
        self.assertEqual(len(self.gateway.callback_answers), 5)
        self.assertIn("设置", self.gateway.private_messages[-1]["text"])

    def test_timezone_setup_accepts_city_input(self) -> None:
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 1,
                    "from": {"id": 10002, "first_name": "时区用户"},
                    "chat": {"id": 10002},
                    "text": "/start",
                }
            }
        )
        choose_other = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_tz_2",
                    "from": {"id": 10002, "first_name": "时区用户"},
                    "message": {"chat": {"id": 10002}, "message_id": 11},
                    "data": "timezone:no",
                }
            }
        )
        timezone_set = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 2,
                    "from": {"id": 10002, "first_name": "时区用户"},
                    "chat": {"id": 10002},
                    "text": "manila",
                }
            }
        )

        with self.app.db.transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10002'").fetchone()

        self.assertEqual(choose_other["type"], "callback_timezone_input_requested")
        self.assertEqual(timezone_set["type"], "timezone_set")
        self.assertEqual(timezone_set["timezone"], "Asia/Manila")
        self.assertEqual(account["timezone"], "Asia/Manila")
        self.assertIsNotNone(account["timezone_confirmed_at"])

    def test_home_ad_launch_selects_creative_slot_then_channel(self) -> None:
        low_channel = self.bind_channel()
        high_channel = self.app.channels.bind_channel(
            telegram_chat_id=-100456,
            title="高订阅频道",
            username="high_channel",
            owner_telegram_user_id=20002,
            owner_display_name="频道主2",
        )
        self.app.pricing.assess_channel(
            channel_id=low_channel["id"],
            category="general",
            median_24h_views=200,
            subscribers=1_000,
            light_clicks_30d=5,
            light_unique_clickers_30d=3,
            repeat_purchase_count=0,
            dispute_count=0,
        )
        self.app.pricing.assess_channel(
            channel_id=high_channel["id"],
            category="general",
            median_24h_views=10_000,
            subscribers=50_000,
            light_clicks_30d=80,
            light_unique_clickers_30d=40,
            repeat_purchase_count=1,
            dispute_count=0,
        )
        self.confirm_timezone(10001, display_name="广告主")
        with self.app.db.transaction() as conn:
            account = self.app.update_handler.accounts.get_or_create_by_telegram(conn, 10001, "mixed", "广告主")
            conn.execute(
                "INSERT INTO campaigns (id, advertiser_account_id, name, status) VALUES ('camp_launch', ?, '记账工具B版', 'active')",
                (account["id"],),
            )
            conn.execute(
                """
                INSERT INTO creatives (
                    id, campaign_id, text, target_url, button_text, short_text, standard_text,
                    media_file_id, media_type, status, content_hash
                )
                VALUES (
                    'cre_launch', 'camp_launch', '记账工具完整介绍', 'https://launch.example',
                    '查看', '记账工具', '记账工具标准文案', 'photo_large', 'photo',
                    'approved', 'hash_launch'
                )
                """
            )
            collection = self.app.update_handler._ensure_channel_collection_conn(conn, account["id"], "高订阅组合")
            self.app.update_handler._save_channel_to_collection_conn(conn, account["id"], collection["id"], high_channel["id"])

        start = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_launch_start",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "advertiser:order_help",
                }
            }
        )
        creative_page = self.gateway.private_messages[-1]
        pick = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_launch_pick",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:pick:0",
                }
            }
        )
        display_page = self.gateway.private_messages[-1]
        slot = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_launch_slot",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:slot:standard_card",
                }
            }
        )
        folder_page = self.gateway.private_messages[-1]
        folder_buttons = [button["text"] for row in folder_page["inline_keyboard"] for button in row]
        manual = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_launch_manual_channels",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:channel_mode:channels",
                }
            }
        )
        channel_page = self.gateway.private_messages[-1]
        channel_buttons = [button["text"] for row in channel_page["inline_keyboard"] for button in row]
        toggled = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_launch_toggle",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:toggle:0",
                }
            }
        )
        toggled_page = self.gateway.private_messages[-1]
        toggled_buttons = [button["text"] for row in toggled_page["inline_keyboard"] for button in row]
        selected = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_launch_start",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:start",
                }
            }
        )
        schedule_page = self.gateway.private_messages[-1]
        back = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_launch_back",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:back",
                }
            }
        )

        self.assertEqual(start["type"], "callback_global_placement_started")
        self.assertIn("第 1/5 步：选择广告素材", creative_page["text"])
        self.assertIn("➕ 添加广告素材", [button["text"] for row in creative_page["inline_keyboard"] for button in row])
        self.assertEqual(pick["type"], "callback_global_placement_creative_selected")
        self.assertIn("第 2/5 步：选择插播位置", display_page["text"])
        self.assertIn("广告素材：记账工具B版", display_page["text"])
        self.assertEqual(slot["type"], "callback_global_placement_slot")
        self.assertIn("第 3/5 步：选择频道文件夹", folder_page["text"])
        self.assertIn("默认按频道文件夹投放", folder_page["text"])
        self.assertIn("高订阅组合", folder_page["text"])
        self.assertIn("可投 1", folder_page["text"])
        self.assertIn("📺 按频道选择", folder_buttons)
        self.assertIn("➕ 新建文件夹", folder_buttons)
        self.assertIn("下一步 ➡️（1 个频道）", folder_buttons)
        self.assertEqual(manual["type"], "callback_global_placement_channel_mode")
        self.assertEqual(manual["mode"], "channels")
        self.assertIn("第 3/5 步：选择投放频道", channel_page["text"])
        self.assertIn("当前显示 1-2 / 2", channel_page["text"])
        self.assertIn("频道清单", channel_page["text"])
        self.assertIn("1. 👥 5.0万｜高订阅频道｜USD 10.00", channel_page["text"])
        self.assertIn("https://t.me/high_channel", channel_page["text"])
        self.assertTrue(any("1 高订阅频道" in button for button in channel_buttons))
        self.assertEqual(toggled["type"], "callback_global_placement_channel_toggled")
        self.assertEqual(toggled["selected_count"], 1)
        self.assertIn("频道：高订阅频道", toggled_page["text"])
        self.assertIn("✅ 1 高订阅频道", toggled_buttons)
        self.assertIn("✅ 开始投放（已选 1 个）", toggled_buttons)
        self.assertEqual(selected["type"], "callback_global_placement_channel_selected")
        self.assertEqual(selected["channel_ids"], [high_channel["id"]])
        self.assertEqual(selected["panel"], "schedule")
        self.assertIn("🎯 给「高订阅频道」投放广告", schedule_page["text"])
        self.assertIn("频道：高订阅频道", schedule_page["text"])
        self.assertIn("第 4/5 步：配置发布节奏", schedule_page["text"])
        self.assertEqual(back["panel"], "channel")
        self.assertIn("第 3/5 步：选择投放频道", self.gateway.private_messages[-1]["text"])

    def test_home_ad_launch_can_submit_multiple_channels(self) -> None:
        first_channel = self.bind_channel()
        second_channel = self.app.channels.bind_channel(
            telegram_chat_id=-100789,
            title="第二投放频道",
            username="second_launch_channel",
            owner_telegram_user_id=20002,
            owner_display_name="频道主2",
        )
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("30")
        with self.app.db.transaction() as conn:
            account = self.app.update_handler.accounts.get_or_create_by_telegram(conn, 10001, "mixed", "广告主")
            conn.execute(
                "INSERT INTO campaigns (id, advertiser_account_id, name, status) VALUES ('camp_multi_launch', ?, '批量素材', 'active')",
                (account["id"],),
            )
            conn.execute(
                """
                INSERT INTO creatives (
                    id, campaign_id, text, target_url, button_text, short_text, standard_text,
                    media_file_id, media_type, status, content_hash
                )
                VALUES (
                    'cre_multi_launch', 'camp_multi_launch', '批量投放详情', 'https://multi.example',
                    '查看', '批量广告', '批量标准文案', 'photo_large', 'photo',
                    'approved', 'hash_multi_launch'
                )
                """
            )
            collection = self.app.update_handler._ensure_channel_collection_conn(conn, account["id"], "批量频道组")
            self.app.update_handler._save_channel_to_collection_conn(conn, account["id"], collection["id"], first_channel["id"])
            self.app.update_handler._save_channel_to_collection_conn(conn, account["id"], collection["id"], second_channel["id"])

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_multi_start",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "advertiser:order_help",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_multi_pick",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:pick:0",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_multi_slot",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:slot:standard_card",
                }
            }
        )
        selected_page = self.gateway.private_messages[-1]
        selected = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_multi_begin",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:start",
                }
            }
        )
        schedule_page = self.gateway.private_messages[-1]
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_multi_next",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:next",
                }
            }
        )
        confirm_page = self.gateway.private_messages[-1]
        submit = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_multi_submit",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:submit",
                }
            }
        )

        with self.app.db.transaction() as conn:
            orders = conn.execute("SELECT * FROM ad_orders ORDER BY created_at, id").fetchall()
            reserved = conn.execute("SELECT reserved_balance_cents FROM accounts WHERE telegram_user_id = '10001'").fetchone()

        self.assertIn("第 3/5 步：选择频道文件夹", selected_page["text"])
        self.assertIn("频道：批量频道组（2 个频道）", selected_page["text"])
        self.assertIn("下一步 ➡️（2 个频道）", [button["text"] for row in selected_page["inline_keyboard"] for button in row])
        self.assertEqual(selected["type"], "callback_global_placement_channel_selected")
        self.assertEqual(set(selected["channel_ids"]), {first_channel["id"], second_channel["id"]})
        self.assertIn("🎯 给「批量频道组（2 个频道）」投放广告", schedule_page["text"])
        self.assertIn("预算：预计 USD 20.00", schedule_page["text"])
        self.assertIn("需要冻结：USD 20.00", confirm_page["text"])
        self.assertEqual(submit["type"], "callback_placement_batch_created")
        self.assertEqual(len(orders), 2)
        self.assertEqual({orders[0]["channel_id"], orders[1]["channel_id"]}, {first_channel["id"], second_channel["id"]})
        self.assertEqual(sum(order["budget_cents"] for order in orders), 2000)
        self.assertEqual(reserved["reserved_balance_cents"], 2000)

    def test_channel_market_saves_folder_and_launches_from_collection(self) -> None:
        low_channel = self.bind_channel()
        high_channel = self.app.channels.bind_channel(
            telegram_chat_id=-100987,
            title="优质动漫频道",
            username="anime_channel",
            owner_telegram_user_id=20002,
            owner_display_name="频道主2",
        )
        self.app.pricing.assess_channel(
            channel_id=low_channel["id"],
            category="general",
            median_24h_views=200,
            subscribers=1_000,
            light_clicks_30d=5,
            light_unique_clickers_30d=3,
            repeat_purchase_count=0,
            dispute_count=0,
        )
        self.app.pricing.assess_channel(
            channel_id=high_channel["id"],
            category="anime",
            median_24h_views=8_000,
            subscribers=80_000,
            light_clicks_30d=80,
            light_unique_clickers_30d=40,
            repeat_purchase_count=1,
            dispute_count=0,
        )
        self.confirm_timezone(10001, display_name="广告主")
        with self.app.db.transaction() as conn:
            account = self.app.update_handler.accounts.get_or_create_by_telegram(conn, 10001, "mixed", "广告主")
            conn.execute(
                "INSERT INTO campaigns (id, advertiser_account_id, name, status) VALUES ('camp_market', ?, '频道广场素材', 'active')",
                (account["id"],),
            )
            conn.execute(
                """
                INSERT INTO creatives (
                    id, campaign_id, text, target_url, button_text, short_text, standard_text,
                    media_file_id, media_type, status, content_hash
                )
                VALUES (
                    'cre_market', 'camp_market', '频道广场投放详情', 'https://market.example',
                    '查看', '广场广告', '频道广场标准文案', 'photo_large', 'photo',
                    'approved', 'hash_market'
                )
                """
            )

        home = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_home",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "market:home",
                }
            }
        )
        home_page = self.gateway.private_messages[-1]
        browse = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_browse",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "market:browse",
                }
            }
        )
        browse_page = self.gateway.private_messages[-1]
        browse_buttons = [button["text"] for row in browse_page["inline_keyboard"] for button in row]
        new_folder = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_new",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "market:new_folder",
                }
            }
        )
        created = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 9001,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "动漫频道",
                }
            }
        )
        saved = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_save",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "market:save",
                }
            }
        )
        saved_page = self.gateway.private_messages[-1]
        saved_buttons = [button["text"] for row in saved_page["inline_keyboard"] for button in row]
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_next_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "market:next",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_next_2",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "market:next",
                }
            }
        )
        end_page = self.gateway.private_messages[-1]
        end_buttons = [button["text"] for row in end_page["inline_keyboard"] for button in row]
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_folders",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "market:folders",
                }
            }
        )
        folders_page = self.gateway.private_messages[-1]
        folder_buttons = [button["text"] for row in folders_page["inline_keyboard"] for button in row]
        folder_callback = next(
            button["callback_data"]
            for row in folders_page["inline_keyboard"]
            for button in row
            if "动漫频道" in button["text"]
        )
        selected_folder = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_folder",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": folder_callback,
                }
            }
        )
        selected_folder_page = self.gateway.private_messages[-1]
        launch = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_launch",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "market:launch_folder:0",
                }
            }
        )
        creative_page = self.gateway.private_messages[-1]
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_pick",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:pick:0",
                }
            }
        )
        slot = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_market_slot",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "launch:slot:standard_card",
                }
            }
        )
        schedule_page = self.gateway.private_messages[-1]

        self.assertEqual(home["type"], "callback_channel_market_browse")
        self.assertIn("第 1/2 个频道", home_page["text"])
        self.assertIn("优质动漫频道", home_page["text"])
        self.assertNotIn("像刷频道卡片一样挑选投放目标", home_page["text"])
        self.assertNotIn("🔎 开始刷频道", [button["text"] for row in home_page["inline_keyboard"] for button in row])
        self.assertEqual(browse["type"], "callback_channel_market_browse")
        self.assertIn("第 1/2 个频道", browse_page["text"])
        self.assertIn("优质动漫频道", browse_page["text"])
        self.assertIn("https://t.me/anime_channel", browse_page["text"])
        self.assertIn("标准插播", browse_page["text"])
        self.assertEqual(browse_page["parse_mode"], "HTML")
        self.assertIn("<pre>", browse_page["text"])
        self.assertIn("<blockquote>", browse_page["text"])
        self.assertIn("📁 默认收藏夹", browse_buttons)
        self.assertIn("⭐ 收藏", browse_buttons)
        self.assertNotIn("➕ 广告投放", browse_buttons)
        self.assertNotIn("🏠 主菜单", browse_buttons)
        self.assertNotIn("➕ 新建收藏夹", browse_buttons)
        self.assertEqual(new_folder["type"], "callback_channel_market_new_folder")
        self.assertEqual(created["type"], "channel_market_folder_created")
        self.assertEqual(saved["type"], "callback_channel_market_saved")
        self.assertIn("当前收藏夹：动漫频道（1 个）", saved_page["text"])
        self.assertIn("状态：已在当前收藏夹", saved_page["text"])
        self.assertIn("📁 动漫频道", saved_buttons)
        self.assertIn("✅ 已收藏", saved_buttons)
        self.assertIn("已经看到最后一个频道", end_page["text"])
        self.assertIn("📣 推荐给频道主", end_buttons)
        self.assertIn("🏠 返回主页", end_buttons)
        self.assertIn("动漫频道｜1 个频道", folders_page["text"])
        self.assertIn("选择一个收藏夹后，会直接回到频道广场继续刷。", folders_page["text"])
        self.assertNotIn("🏠 主菜单", folder_buttons)
        self.assertEqual(selected_folder["type"], "callback_channel_market_folder_selected")
        self.assertIn("🔎 频道广场", selected_folder_page["text"])
        self.assertIn("当前收藏夹：动漫频道（1 个）", selected_folder_page["text"])
        self.assertNotIn("这个收藏夹还是空的", selected_folder_page["text"])
        self.assertEqual(launch["type"], "callback_channel_market_launch_collection")
        self.assertIn("频道：动漫频道（1 个）", creative_page["text"])
        self.assertEqual(slot["type"], "callback_global_placement_collection_slot")
        self.assertIn("第 4/5 步：配置发布节奏", schedule_page["text"])

    def test_start_opens_main_menu_and_channel_management_lists_assets(self) -> None:
        for index in range(12):
            self.app.channels.bind_channel(
                telegram_chat_id=-100900 - index,
                title=f"频道{index + 1}",
                username=f"channel_{index + 1}",
                owner_telegram_user_id=20001,
                owner_display_name="频道主",
            )
        self.confirm_timezone(20001, role="publisher", display_name="频道主")

        start = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 1,
                    "from": {"id": 20001, "first_name": "频道主"},
                    "chat": {"id": 20001},
                    "text": "/start",
                }
            }
        )

        self.assertEqual(start["type"], "organic_start")
        self.assertIn("插播广告工作台", self.gateway.private_messages[-1]["text"])
        self.assertNotIn("频道资产", self.gateway.private_messages[-1]["text"])
        channels = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_start_channels",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}},
                    "data": "publisher:channels",
                }
            }
        )
        message = self.gateway.private_messages[-1]
        keyboard = message["inline_keyboard"]
        channel_buttons = [button for row in keyboard for button in row if button.get("callback_data", "").startswith("pub:channel:")]

        self.assertEqual(channels["type"], "callback_publisher_channels")
        self.assertIn("12 个频道资产", message["text"])
        self.assertIn("先显示前 10 个", message["text"])
        self.assertEqual(len(channel_buttons), 10)
        self.assertTrue(all(len(row) == 2 for row in keyboard[:5]))
        self.assertEqual(keyboard[-1], [{"text": "🏠 返回主菜单", "callback_data": "menu:home"}])

    def test_my_chat_member_binds_channel_syncs_admins_and_notifies(self) -> None:
        channel_chat_id = -100888
        self.gateway.chat_administrators[str(channel_chat_id)] = [
            {
                "status": "creator",
                "user": {"id": 20001, "first_name": "频道主", "is_bot": False},
            },
            {
                "status": "administrator",
                "user": {"id": 20002, "first_name": "副管理员", "is_bot": False},
                "can_post_messages": True,
                "can_edit_messages": True,
            },
            {
                "status": "administrator",
                "user": {"id": self.gateway.bot_user["id"], "username": "ChaBoTestBot", "is_bot": True},
                "can_post_messages": True,
                "can_edit_messages": True,
                "can_pin_messages": True,
            },
        ]

        result = self.app.update_handler.handle(
            {
                "my_chat_member": {
                    "chat": {"id": channel_chat_id, "type": "channel", "title": "资产频道", "username": "asset_channel"},
                    "from": {"id": 20001, "first_name": "频道主", "is_bot": False},
                    "old_chat_member": {"status": "left"},
                    "new_chat_member": {
                        "status": "administrator",
                        "user": self.gateway.bot_user,
                        "can_post_messages": True,
                        "can_edit_messages": True,
                        "can_pin_messages": True,
                    },
                }
            }
        )

        with self.app.db.transaction() as conn:
            channel = conn.execute("SELECT * FROM channels WHERE telegram_chat_id = ?", (str(channel_chat_id),)).fetchone()
            admins = conn.execute("SELECT * FROM channel_admins WHERE channel_id = ? ORDER BY telegram_user_id", (channel["id"],)).fetchall()

        self.assertEqual(result["type"], "bot_channel_asset_added")
        self.assertEqual(result["admins"], 3)
        self.assertEqual(channel["title"], "资产频道")
        self.assertEqual(len(admins), 3)
        self.assertGreaterEqual(result["notified"], 2)
        notification = next(
            message
            for message in self.gateway.private_messages
            if message["chat_id"] == "20002" and "频道已加入插播" in message["text"]
        )
        keyboard = notification["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "⚙️ 频道设置")
        self.assertEqual(keyboard[0][1]["text"], "📋 使用模版")
        self.assertTrue(keyboard[0][0]["callback_data"].startswith("pub:channel:"))
        self.assertTrue(keyboard[0][1]["callback_data"].startswith("pub:template:"))
        self.assertNotIn("📺 频道管理", [button["text"] for row in keyboard for button in row])

    def test_publisher_can_apply_channel_template_to_new_channel(self) -> None:
        source = self.app.channels.bind_channel(
            telegram_chat_id=-100901,
            title="模板频道",
            username="template_channel",
            owner_telegram_user_id=20001,
            owner_display_name="频道主",
        )
        target = self.app.channels.bind_channel(
            telegram_chat_id=-100902,
            title="710",
            username="target_channel",
            owner_telegram_user_id=20001,
            owner_display_name="频道主",
        )
        self.app.channels.update_rate(source["id"], "standard_card", 4200)
        self.app.channels.set_format_policy(source["id"], "strong_post", enabled=False)
        with self.app.db.transaction() as conn:
            conn.execute(
                """
                UPDATE channel_configs
                SET daily_ad_limit = 8,
                    allowed_start_hour = 10,
                    allowed_end_hour = 22,
                    allow_pin = 0,
                    service_fee_bps = 700,
                    holdback_bps = 1200,
                    holdback_days = 5
                WHERE channel_id = ?
                """,
                (source["id"],),
            )
            conn.execute(
                "UPDATE ad_slots SET enabled = 0, min_days = 3 WHERE channel_id = ? AND slot_type = 'light_tail'",
                (source["id"],),
            )

        picker = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_tpl_pick",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}},
                    "data": f"pub:template:{target['ref_token']}",
                }
            }
        )
        picker_message = self.gateway.private_messages[-1]
        picker_buttons = [button for row in picker_message["inline_keyboard"] for button in row]

        applied = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_tpl_apply",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}},
                    "data": f"pub:tplapply:{target['ref_token']}:{source['ref_token']}",
                }
            }
        )

        with self.app.db.transaction() as conn:
            target_config = conn.execute("SELECT * FROM channel_configs WHERE channel_id = ?", (target["id"],)).fetchone()
            strong_policy = conn.execute(
                "SELECT * FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = 'strong_post'",
                (target["id"],),
            ).fetchone()
            standard_rate = conn.execute(
                """
                SELECT r.*
                FROM rate_cards r
                JOIN ad_slots s ON s.id = r.slot_id
                WHERE s.channel_id = ? AND s.slot_type = 'standard_card' AND r.active = 1
                """,
                (target["id"],),
            ).fetchone()
            light_slot = conn.execute(
                "SELECT * FROM ad_slots WHERE channel_id = ? AND slot_type = 'light_tail'",
                (target["id"],),
            ).fetchone()

        self.assertEqual(picker["type"], "callback_publisher_template_picker")
        self.assertIn("目标频道：710", picker_message["text"])
        self.assertTrue(any(button["text"] == "📋 模板频道" for button in picker_buttons))
        self.assertEqual(applied["type"], "callback_publisher_template_applied")
        self.assertIn("模版已应用", self.gateway.private_messages[-1]["text"])
        self.assertEqual(target_config["daily_ad_limit"], 8)
        self.assertEqual(target_config["allowed_start_hour"], 10)
        self.assertEqual(target_config["allowed_end_hour"], 22)
        self.assertEqual(target_config["allow_pin"], 0)
        self.assertEqual(target_config["service_fee_bps"], 700)
        self.assertEqual(target_config["holdback_bps"], 1200)
        self.assertEqual(target_config["holdback_days"], 5)
        self.assertEqual(strong_policy["enabled"], 0)
        self.assertEqual(standard_rate["unit_price_cents"], 4200)
        self.assertEqual(light_slot["enabled"], 0)
        self.assertEqual(light_slot["min_days"], 3)

    def test_bot_self_serve_order_form_creates_pending_order(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")

        started = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_order_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"channel:order:{channel['id']}",
                }
            }
        )
        slot = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_order_2",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"order:slot:{channel['id']}:standard_card",
                }
            }
        )
        creative = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 20,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "这是一个自助创建的插播广告",
                }
            }
        )
        url = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 21,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "https://selfserve.example",
                }
            }
        )
        budget = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 22,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "12",
                }
            }
        )

        with self.app.db.transaction() as conn:
            order = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (budget["order_id"],)).fetchone()
            creative_row = conn.execute("SELECT * FROM creatives WHERE id = ?", (order["creative_id"],)).fetchone()
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            state = conn.execute("SELECT * FROM bot_conversation_states WHERE chat_id = '10001'").fetchone()

        self.assertEqual(started["type"], "callback_order_flow_started")
        self.assertEqual(slot["type"], "callback_order_slot_selected")
        self.assertEqual(creative["type"], "order_form_creative_saved")
        self.assertEqual(url["type"], "order_form_url_saved")
        self.assertEqual(budget["type"], "order_form_order_created")
        self.assertEqual(order["status"], "approved")
        self.assertEqual(order["reserved_cents"], 1200)
        self.assertEqual(order["unit_price_cents"], 1000)
        self.assertEqual(creative_row["target_url"], "https://selfserve.example")
        self.assertIsNotNone(delivery)
        self.assertEqual(advertiser["available_balance_cents"], 800)
        self.assertEqual(advertiser["reserved_balance_cents"], 1200)
        self.assertIsNone(state)

    def test_order_flow_guides_existing_creative_before_budget(self) -> None:
        channel, _order = self.create_approved_order(slot_type="standard_card", budget="20")

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pick_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"channel:order:{channel['id']}",
                }
            }
        )
        slot = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pick_2",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"order:slot:{channel['id']}:standard_card",
                }
            }
        )
        picker = self.gateway.private_messages[-1]
        self.assertEqual(slot["type"], "callback_order_slot_selected")
        self.assertIn("选择广告素材", picker["text"])
        button_texts = [button["text"] for row in picker["inline_keyboard"] for button in row]
        self.assertTrue(any(text.startswith("📄 这是一条插播广告") for text in button_texts))
        self.assertIn("➕ 新建广告", button_texts)

        picked = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pick_3",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "order:pick:0",
                }
            }
        )

        self.assertEqual(picked["type"], "callback_order_creative_selected")
        self.assertIn("第三步：设置本次预算", self.gateway.private_messages[-1]["text"])

    def test_light_tail_order_collects_short_entry_and_full_detail(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("5")

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_light_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"channel:order:{channel['id']}",
                }
            }
        )
        slot = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_light_2",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"order:slot:{channel['id']}:light_tail",
                }
            }
        )
        short_text = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 30,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "领资料",
                }
            }
        )
        detail = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 31,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "这里是轻插播点击后展示的完整广告详情。",
                }
            }
        )
        url = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 32,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "https://light.example",
                }
            }
        )
        budget = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 33,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "3",
                }
            }
        )

        with self.app.db.transaction() as conn:
            order = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (budget["order_id"],)).fetchone()
            creative = conn.execute("SELECT * FROM creatives WHERE id = ?", (order["creative_id"],)).fetchone()

        self.assertEqual(slot["type"], "callback_order_slot_selected")
        self.assertTrue(any("15 个字以内" in message["text"] for message in self.gateway.private_messages))
        self.assertEqual(short_text["type"], "order_form_light_short_text_saved")
        self.assertEqual(detail["type"], "order_form_light_detail_saved")
        self.assertEqual(url["type"], "order_form_url_saved")
        self.assertEqual(order["status"], "approved")
        self.assertEqual(order["unit_price_cents"], 300)
        self.assertEqual(creative["button_text"], "领资料")
        self.assertEqual(creative["text"], "这里是轻插播点击后展示的完整广告详情。")

        self.app.update_handler.handle(
            {
                "channel_post": {
                    "message_id": 88,
                    "chat": {"id": -100123, "title": "测试频道"},
                    "text": "频道最新帖子",
                }
            }
        )
        dispatched = self.app.fulfillment.dispatch_due()
        self.assertEqual(dispatched[0]["status"], "sent")
        self.assertEqual(dispatched[0]["message_id"], "88")
        self.assertEqual(self.gateway.channel_text_edits[-1]["text"], "频道最新帖子\n\n🔖 领资料")
        self.assertEqual(self.gateway.channel_text_edits[-1]["inline_keyboard"][-1][0]["text"], "查看完整广告")
        self.assertEqual(self.gateway.sent_ads, [])

        with self.app.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 34,
                    "from": {"id": 333, "first_name": "点击用户"},
                    "chat": {"id": 333},
                    "text": f"/start ad_{delivery['id']}",
                }
            }
        )
        self.assertEqual(result["type"], "ad_start")
        self.assertIn("完整广告详情", self.gateway.private_messages[-1]["text"])
        self.assertIn("https://light.example", self.gateway.private_messages[-1]["text"])

    def test_account_becomes_mixed_when_same_user_is_advertiser_and_publisher(self) -> None:
        with self.app.db.transaction() as conn:
            advertiser = self.app.update_handler.accounts.get_or_create_by_telegram(conn, 10001, "advertiser", "同一用户")
            publisher = self.app.update_handler.accounts.get_or_create_by_telegram(conn, 10001, "publisher", "同一用户")

        self.assertEqual(advertiser["id"], publisher["id"])
        self.assertEqual(publisher["role"], "mixed")

    def test_polling_runner_logs_get_updates_error_without_crashing_once(self) -> None:
        class FailingGateway:
            def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
                return True

            def get_updates(self, *, offset=None, timeout=30, limit=100):
                raise TelegramError("poll timeout")

        runner = PollingRunner(
            Settings(
                db_path=str(Path(self.tmp.name) / "polling.sqlite3"),
                bot_token="dummy",
                bot_username="ChaBoTestBot",
            )
        )
        runner.gateway = FailingGateway()
        logs = []

        runner.run(once=True, idle_sleep_seconds=0, log=logs.append)

        self.assertTrue(any('"event": "polling_error"' in item for item in logs))

    def test_polling_runner_dispatches_due_orders_after_update(self) -> None:
        self.create_approved_order(budget="10")

        class PollingGateway(FakeGateway):
            def __init__(self) -> None:
                super().__init__()
                self.deleted_webhook = False

            def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
                self.deleted_webhook = True
                return True

            def get_updates(self, *, offset=None, timeout=30, limit=100):
                return [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 1,
                            "from": {"id": 10001, "first_name": "广告主"},
                            "chat": {"id": 10001},
                            "text": "/start",
                        },
                    }
                ]

        runner_settings = Settings(
            db_path=self.settings.db_path,
            bot_token="dummy",
            bot_username="ChaBoTestBot",
        )
        runner = PollingRunner(runner_settings)
        polling_gateway = PollingGateway()
        runner.gateway = polling_gateway
        runner.handler = UpdateHandler(runner.db, runner.settings, polling_gateway)
        runner.fulfillment = FulfillmentService(runner.db, runner.settings, polling_gateway)
        logs = []

        runner.run(once=True, idle_sleep_seconds=0, log=logs.append)

        self.assertTrue(polling_gateway.deleted_webhook)
        self.assertEqual(len(polling_gateway.sent_ads), 1)
        self.assertTrue(any('"event": "dispatch_due"' in item for item in logs))
        with runner.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries").fetchone()
            order = conn.execute("SELECT * FROM ad_orders").fetchone()
        self.assertEqual(delivery["status"], "sent")
        self.assertEqual(order["spent_cents"], 1000)

    def test_http_webhook_and_admin_actions_are_runnable(self) -> None:
        base_url = self.start_http_server()
        channel = self.bind_channel()

        unauthorized = urllib.request.Request(f"{base_url}/admin", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(unauthorized, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

        webhook = self.http_json(
            "POST",
            f"{base_url}/telegram/webhook/webhook-secret",
            {
                "channel_post": {
                    "message_id": 70,
                    "chat": {"id": -100123, "title": "测试频道"},
                    "text": "HTTP webhook 新帖",
                }
            },
        )
        self.assertTrue(webhook["ok"])
        self.assertEqual(self.gateway.edits[-1]["inline_keyboard"][-1][0]["text"], "📣 频道招商")

        self.topup_advertiser("20")
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            text="HTTP 后台审核的插播广告",
            target_url="https://admin.example",
            budget_cents=money_to_cents("12"),
        )

        orders = self.http_json("GET", f"{base_url}/admin/orders?token=admin-token&status=pending_review")
        self.assertEqual(orders["orders"][0]["id"], order["id"])

        approved = self.http_json("POST", f"{base_url}/admin/orders/{order['id']}/approve?token=admin-token", {})
        self.assertEqual(approved["result"]["status"], "approved")

        dispatched = self.http_json("POST", f"{base_url}/admin/dispatch-due?token=admin-token", {"limit": 5})
        self.assertEqual(dispatched["result"][0]["status"], "sent")
        self.assertEqual(self.gateway.sent_ads[-1]["chat_id"], str(channel["telegram_chat_id"]))

        with self.app.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
        order_detail = self.http_json("GET", f"{base_url}/admin/orders/{order['id']}?token=admin-token")
        self.assertEqual(order_detail["order"]["订单"]["id"], order["id"])
        self.assertGreaterEqual(len(order_detail["order"]["证据链"]), 1)
        partial = self.http_json(
            "POST",
            f"{base_url}/admin/deliveries/{delivery['id']}/refund?token=admin-token",
            {"amount": "4", "reason": "HTTP 后台部分退款"},
        )
        self.assertEqual(partial["result"]["delivery"]["status"], "sent")
        self.assertEqual(partial["result"]["delivery"]["refunded_cents"], 400)

        dispute = self.app.disputes.open_dispute(
            opened_by_telegram_user_id=10001,
            delivery_id=delivery["id"],
            reason="HTTP 后台退款测试",
        )
        refunded = self.http_json(
            "POST",
            f"{base_url}/admin/deliveries/{delivery['id']}/refund?token=admin-token",
            {"reason": "HTTP 后台退款"},
        )
        self.assertEqual(refunded["result"]["delivery"]["status"], "refunded")
        with self.app.db.transaction() as conn:
            saved_dispute = conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute["id"],)).fetchone()
        self.assertEqual(saved_dispute["status"], "resolved")

    def test_publisher_onboarding_binds_forwarded_channel_and_toggles_format(self) -> None:
        channel_chat_id = -100777
        self.gateway.chat_members[(str(channel_chat_id), "20001")] = {"status": "creator"}
        self.gateway.chat_members[(str(channel_chat_id), str(self.gateway.bot_user["id"]))] = {
            "status": "administrator",
            "can_post_messages": True,
            "can_edit_messages": True,
            "can_pin_messages": True,
        }

        started = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pub_1",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}},
                    "data": "publisher:onboard",
                }
            }
        )
        self.assertEqual(self.gateway.private_messages[-1]["inline_keyboard"][0][0]["text"], "❌ 取消接入")
        bound = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 30,
                    "from": {"id": 20001, "first_name": "频道主"},
                    "chat": {"id": 20001},
                    "forward_origin": {
                        "type": "channel",
                        "chat": {"id": channel_chat_id, "title": "新接入频道", "username": "new_channel"},
                        "message_id": 8,
                    },
                }
            }
        )

        with self.app.db.transaction() as conn:
            channel = conn.execute("SELECT * FROM channels WHERE telegram_chat_id = ?", (str(channel_chat_id),)).fetchone()
            state = conn.execute("SELECT * FROM bot_conversation_states WHERE chat_id = '20001'").fetchone()

        formats = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pub_2",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}},
                    "data": f"pub:formats:{channel['ref_token']}",
                }
            }
        )
        quote = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pub_quote",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}},
                    "data": f"channel:quote:{channel['id']}",
                }
            }
        )
        quote_buttons = [button["text"] for row in self.gateway.private_messages[-1]["inline_keyboard"] for button in row]
        toggled = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pub_3",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}},
                    "data": f"pub:toggle:{channel['ref_token']}:strong_post",
                }
            }
        )

        with self.app.db.transaction() as conn:
            strong_policy = conn.execute(
                "SELECT * FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = 'strong_post'",
                (channel["id"],),
            ).fetchone()

        self.assertEqual(started["type"], "callback_publisher_onboard_started")
        self.assertEqual(bound["type"], "publisher_channel_bound")
        self.assertEqual(channel["title"], "新接入频道")
        self.assertEqual(channel["username"], "new_channel")
        self.assertIsNone(state)
        self.assertTrue(any("频道已接入" in message["text"] for message in self.gateway.private_messages))
        self.assertEqual(formats["type"], "callback_publisher_formats")
        self.assertEqual(quote["type"], "callback_channel_quote")
        self.assertIn("⬅️ 频道详情", quote_buttons)
        self.assertNotIn("📣 投放这个频道", quote_buttons)
        self.assertEqual(toggled["type"], "callback_publisher_format_toggled")
        self.assertEqual(strong_policy["enabled"], 0)
        self.assertIn("定制插播", self.gateway.private_messages[-1]["text"])
        self.assertNotIn("🔁 循环发布｜", self.gateway.private_messages[-1]["text"])
        self.assertNotIn("📌 置顶 24h｜", self.gateway.private_messages[-1]["text"])
        self.assertNotIn("strong_post", self.gateway.private_messages[-1]["text"])

    def test_admin_reject_order_releases_reserved_budget(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            text="需要审核的插播广告",
            target_url="https://reject.example",
            budget_cents=money_to_cents("10"),
        )

        rejected = self.app.orders.reject_order(order["id"], "素材不符合插播规范")

        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            creative = conn.execute("SELECT * FROM creatives WHERE id = ?", (order["creative_id"],)).fetchone()

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reserved_cents"], 0)
        self.assertEqual(creative["status"], "rejected")
        self.assertEqual(advertiser["available_balance_cents"], 2000)
        self.assertEqual(advertiser["reserved_balance_cents"], 0)

    def test_admin_refund_delivery_reverses_ledger_balances(self) -> None:
        _, order = self.create_approved_order()
        self.app.fulfillment.dispatch_due()
        with self.app.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
        dispute = self.app.disputes.open_dispute(
            opened_by_telegram_user_id=10001,
            delivery_id=delivery["id"],
            reason="频道主提前删除广告",
        )

        refunded = self.app.orders.refund_delivery(delivery["id"], "频道主提前删除广告")

        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            publisher = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            saved_delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery["id"],)).fetchone()
            saved_dispute = conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute["id"],)).fetchone()

        self.assertEqual(refunded["order"]["status"], "refunded")
        self.assertEqual(refunded["order"]["spent_cents"], 0)
        self.assertEqual(saved_delivery["status"], "refunded")
        self.assertEqual(advertiser["available_balance_cents"], 1000)
        self.assertEqual(advertiser["reserved_balance_cents"], 0)
        self.assertEqual(advertiser["spent_balance_cents"], 0)
        self.assertEqual(publisher["pending_earnings_cents"], 0)
        self.assertEqual(saved_dispute["status"], "resolved")
        self.assertIn("已退款", saved_dispute["resolution"])

    def test_partial_refund_reverses_only_part_and_confirm_uses_remaining_earning(self) -> None:
        _, order = self.create_approved_order()
        self.app.fulfillment.dispatch_due()
        with self.app.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()

        refunded = self.app.orders.refund_delivery_partial(delivery["id"], money_to_cents("4"), "部分退款")
        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            publisher = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            saved_delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery["id"],)).fetchone()
            conn.execute("UPDATE deliveries SET sent_at = datetime('now', '-2 hours') WHERE id = ?", (delivery["id"],))

        self.assertEqual(refunded["delivery"]["status"], "sent")
        self.assertEqual(saved_delivery["refunded_cents"], 400)
        self.assertEqual(saved_delivery["publisher_reversed_cents"], 400)
        self.assertEqual(advertiser["available_balance_cents"], 400)
        self.assertEqual(advertiser["spent_balance_cents"], 600)
        self.assertEqual(publisher["pending_earnings_cents"], 600)

        confirmed_count = self.app.fulfillment.confirm_due_earnings(observation_hours=1)
        with self.app.db.transaction() as conn:
            publisher_after = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            confirmed_delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery["id"],)).fetchone()

        self.assertEqual(confirmed_count, 1)
        self.assertEqual(confirmed_delivery["status"], "confirmed")
        self.assertEqual(publisher_after["pending_earnings_cents"], 0)
        self.assertEqual(publisher_after["confirmed_earnings_cents"], 600)
        self.assertEqual(publisher_after["releasable_earnings_cents"], 540)

    def test_light_probe_appends_button_and_records_unique_clicks(self) -> None:
        channel = self.bind_channel()
        probe = self.app.light_probes.create_probe(
            channel_id=channel["id"],
            short_text="想投广告？",
            detail_text="这里是轻插播探针详情",
            target_url="https://probe.example",
            button_text="想投广告？",
        )

        result = self.app.update_handler.handle(
            {
                "channel_post": {
                    "message_id": 8,
                    "chat": {"id": -100123, "title": "测试频道"},
                    "text": "带探针的新帖",
                }
            }
        )

        self.assertTrue(result["handled"])
        keyboard = self.gateway.edits[-1]["inline_keyboard"]
        self.assertEqual(keyboard[-2][0]["text"], "想投广告？")
        self.assertIn(f"probe_{probe['id']}", keyboard[-2][0]["url"])
        self.assertEqual(keyboard[-1][0]["text"], "📣 频道招商")

        for user_id in [333, 333, 444]:
            start = self.app.update_handler.handle(
                {
                    "message": {
                        "message_id": 3,
                        "from": {"id": user_id, "first_name": "点击用户"},
                        "chat": {"id": user_id},
                        "text": f"/start probe_{probe['id']}",
                    }
                }
            )
            self.assertEqual(start["type"], "light_probe_start")

        stats = self.app.light_probes.stats(probe_id=probe["id"])
        self.assertEqual(stats["total_clicks"], 3)
        self.assertEqual(stats["unique_clickers"], 2)
        self.assertIn("https://probe.example", self.gateway.private_messages[-1]["text"])

    def test_successful_delivery_charges_budget_and_records_revenue(self) -> None:
        _, order = self.create_approved_order()

        dispatched = self.app.fulfillment.dispatch_due()

        self.assertEqual(dispatched[0]["status"], "sent")
        self.assertEqual(self.gateway.sent_ads[0]["button_text"], "查看详情")
        self.assertIn("ad_del_", self.gateway.sent_ads[0]["button_url"])

        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            publisher = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            platform = conn.execute("SELECT * FROM accounts WHERE id = 'platform'").fetchone()
            saved_order = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (order["id"],)).fetchone()
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
            ledger_count = conn.execute("SELECT COUNT(*) AS n FROM ledger_transactions").fetchone()["n"]
        notifications = [message for message in self.gateway.private_messages if message["chat_id"] == "20001" and "收入到账" in message["text"]]
        notification_buttons = [button["text"] for row in notifications[0]["inline_keyboard"] for button in row]

        self.assertEqual(advertiser["available_balance_cents"], 0)
        self.assertEqual(advertiser["reserved_balance_cents"], 0)
        self.assertEqual(advertiser["spent_balance_cents"], 1000)
        self.assertEqual(publisher["pending_earnings_cents"], 1000)
        self.assertEqual(platform["available_balance_cents"], 0)
        self.assertEqual(saved_order["status"], "budget_exhausted")
        self.assertEqual(delivery["status"], "sent")
        self.assertGreaterEqual(ledger_count, 4)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["parse_mode"], "HTML")
        self.assertIn("+USD 10.00", notifications[0]["text"])
        self.assertIn('频道：<a href="https://t.me/test_channel/', notifications[0]["text"])
        self.assertIn("广告：", notifications[0]["text"])
        self.assertIn("当前收益：USD 10.00", notifications[0]["text"])
        self.assertEqual(notification_buttons, ["🔕 关闭通知", "💰 我的钱包"])

    def test_publisher_can_disable_and_reenable_income_notifications(self) -> None:
        self.create_approved_order()
        self.app.fulfillment.dispatch_due()

        disabled = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_disable_income_notice",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}, "message_id": 7001},
                    "data": "publisher:disable_income_notifications",
                }
            }
        )
        settings = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_income_settings",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}, "message_id": 7002},
                    "data": "settings:home",
                }
            }
        )
        settings_page = self.gateway.text_edits[-1]
        wallet = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_publisher_wallet",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}, "message_id": 7004},
                    "data": "publisher:earnings",
                }
            }
        )
        wallet_page = self.gateway.text_edits[-1]
        wallet_buttons = [button["text"] for row in wallet_page["inline_keyboard"] for button in row]

        self.create_approved_order()
        self.app.fulfillment.dispatch_due()
        notifications = [message for message in self.gateway.private_messages if message["chat_id"] == "20001" and "收入到账" in message["text"]]

        enabled = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_enable_income_notice",
                    "from": {"id": 20001, "first_name": "频道主"},
                    "message": {"chat": {"id": 20001}, "message_id": 7003},
                    "data": "publisher:enable_income_notifications",
                }
            }
        )

        with self.app.db.transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()

        self.assertEqual(disabled["type"], "callback_publisher_income_notifications_disabled")
        self.assertEqual(settings["type"], "callback_settings_menu")
        self.assertIn("收入通知：已关闭", settings_page["text"])
        self.assertIn("🔔 开启收入通知", [button["text"] for row in settings_page["inline_keyboard"] for button in row])
        self.assertEqual(wallet["type"], "callback_publisher_earnings")
        self.assertIn("💰 我的钱包", wallet_page["text"])
        self.assertEqual(wallet_buttons, ["🔔 开启通知", "💰 我的钱包"])
        self.assertNotIn("📺 频道管理", wallet_buttons)
        self.assertNotIn("🏠 主菜单", wallet_buttons)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(enabled["type"], "callback_publisher_income_notifications_enabled")
        self.assertEqual(account["publisher_income_notifications_enabled"], 1)

    def test_ad_deep_link_records_bot_start_metric(self) -> None:
        _, order = self.create_approved_order()
        self.app.fulfillment.dispatch_due()
        with self.app.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()

        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 2,
                    "from": {"id": 333, "first_name": "点击用户"},
                    "chat": {"id": 333},
                    "text": f"/start ad_{delivery['id']}",
                }
            }
        )

        self.assertEqual(result["type"], "ad_start")
        with self.app.db.transaction() as conn:
            metric = conn.execute("SELECT * FROM metric_snapshots WHERE delivery_id = ?", (delivery["id"],)).fetchone()
        self.assertEqual(metric["metric_type"], "bot_start")
        self.assertIn("https://example.com", self.gateway.private_messages[-1]["text"])

    def test_send_failure_pauses_order_and_releases_budget(self) -> None:
        self.gateway.fail_send = True
        _, order = self.create_approved_order()

        result = self.app.fulfillment.dispatch_due()

        self.assertEqual(result[0]["status"], "failed")
        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            saved_order = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (order["id"],)).fetchone()
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
        self.assertEqual(advertiser["available_balance_cents"], 1000)
        self.assertEqual(advertiser["reserved_balance_cents"], 0)
        self.assertEqual(saved_order["status"], "paused")
        self.assertEqual(delivery["status"], "failed")

    def test_pin_failure_does_not_charge_and_releases_budget(self) -> None:
        self.gateway.fail_pin = True
        _, order = self.create_approved_order(slot_type="pin24h", budget="25")

        result = self.app.fulfillment.dispatch_due()

        self.assertEqual(result[0]["status"], "failed")
        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            publisher = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
        self.assertEqual(advertiser["available_balance_cents"], 2500)
        self.assertEqual(advertiser["spent_balance_cents"], 0)
        self.assertEqual(publisher["pending_earnings_cents"], 0)
        self.assertEqual(delivery["status"], "failed")

    def test_open_dispute_marks_delivery_and_keeps_evidence(self) -> None:
        _, order = self.create_approved_order()
        self.app.fulfillment.dispatch_due()
        with self.app.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()

        dispute = self.app.disputes.open_dispute(
            opened_by_telegram_user_id=10001,
            delivery_id=delivery["id"],
            reason="频道主提前删除插播广告",
        )
        open_disputes = self.app.disputes.list_disputes(status="open")
        resolved = self.app.disputes.resolve_dispute(
            dispute_id=dispute["id"],
            resolution="证据不足，恢复投放记录",
        )

        self.assertEqual(dispute["status"], "open")
        self.assertEqual(open_disputes[0]["id"], dispute["id"])
        self.assertEqual(resolved["status"], "resolved")
        with self.app.db.transaction() as conn:
            saved_delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery["id"],)).fetchone()
            evidence = conn.execute(
                "SELECT * FROM evidence_snapshots WHERE delivery_id = ? AND snapshot_type = 'dispute_opened'",
                (delivery["id"],),
            ).fetchone()
        self.assertEqual(saved_delivery["status"], "sent")
        self.assertIsNotNone(evidence)

    def test_pricing_assessment_quotes_formats_and_applies_rates(self) -> None:
        channel = self.bind_channel()

        assessment = self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="software",
            median_24h_views=20_000,
            subscribers=50_000,
            light_clicks_30d=180,
            light_unique_clickers_30d=120,
            repeat_purchase_count=2,
            dispute_count=0,
            risk_level="normal",
        )
        standard_quote = self.app.pricing.quote_channel(channel["id"], "standard")
        strong_quote = self.app.pricing.quote_channel(channel["id"], "strong_post", "high")

        self.assertGreater(assessment["base_standard_price_cents"], 0)
        self.assertEqual(standard_quote["slot_type"], "standard_card")
        self.assertGreater(strong_quote["list_price_cents"], standard_quote["list_price_cents"])

        applied = self.app.pricing.apply_quotes_to_rate_cards(channel["id"])
        self.assertTrue(any(quote["slot_type"] == "light_tail" for quote in applied))
        with self.app.db.transaction() as conn:
            rate = self.app.channels.get_rate(conn, channel["id"], "standard")
        self.assertEqual(rate["unit_price_cents"], standard_quote["list_price_cents"])

    def test_disabled_ad_format_blocks_orders(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")
        self.app.channels.set_format_policy(channel["id"], "strong_post", enabled=False)

        with self.assertRaisesRegex(Exception, "未开启"):
            self.app.orders.create_order(
                advertiser_telegram_user_id=10001,
                channel_token=channel["ref_token"],
                slot_type="strong_post",
                text="强插播广告",
                target_url="https://example.com",
                budget_cents=money_to_cents("20"),
            )

    def test_advertiser_can_make_and_channel_can_accept_price_offer(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("10")
        self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="news",
            median_24h_views=10_000,
            light_unique_clickers_30d=20,
            risk_level="normal",
        )

        offer = self.app.price_offers.create_offer(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
            slot_type="standard",
            offered_price_cents=300,
            creative_text="砍价插播广告",
            target_url="https://offer.example",
            budget_cents=300,
            message="三美金我马上投",
        )
        accepted = self.app.price_offers.respond_offer(offer["id"], accepted=True)

        self.assertEqual(offer["slot_type"], "standard_card")
        self.assertEqual(offer["status"], "pending")
        self.assertEqual(accepted["status"], "accepted")
        self.assertIsNotNone(accepted["accepted_order_id"])
        with self.app.db.transaction() as conn:
            order = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (accepted["accepted_order_id"],)).fetchone()
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            creative = conn.execute("SELECT * FROM creatives WHERE id = ?", (order["creative_id"],)).fetchone()
        self.assertEqual(order["status"], "pending_review")
        self.assertEqual(order["unit_price_cents"], 300)
        self.assertEqual(order["reserved_cents"], 300)
        self.assertEqual(order["price_offer_id"], offer["id"])
        self.assertEqual(advertiser["available_balance_cents"], 700)
        self.assertEqual(advertiser["reserved_balance_cents"], 300)
        self.assertEqual(creative["target_url"], "https://offer.example")

    def test_accepting_offer_without_balance_keeps_offer_pending(self) -> None:
        channel = self.bind_channel()
        self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="news",
            median_24h_views=10_000,
            light_unique_clickers_30d=20,
            risk_level="normal",
        )
        offer = self.app.price_offers.create_offer(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
            slot_type="standard",
            offered_price_cents=300,
            creative_text="余额不足的砍价广告",
            target_url="https://offer.example",
            budget_cents=300,
        )

        with self.assertRaisesRegex(Exception, "余额不足"):
            self.app.price_offers.respond_offer(offer["id"], accepted=True)
        with self.app.db.transaction() as conn:
            saved_offer = conn.execute("SELECT * FROM price_offers WHERE id = ?", (offer["id"],)).fetchone()
            order_count = conn.execute("SELECT COUNT(*) AS n FROM ad_orders WHERE price_offer_id = ?", (offer["id"],)).fetchone()["n"]
        self.assertEqual(saved_offer["status"], "pending")
        self.assertEqual(order_count, 0)

    def test_publisher_subscription_price_formula_and_premium_gate(self) -> None:
        channel = self.bind_channel()

        self.assertEqual(self.app.subscriptions.quote(1)["monthly_price_cents"], 500)
        self.assertEqual(self.app.subscriptions.quote(5_000)["monthly_price_cents"], 500)
        self.assertEqual(self.app.subscriptions.quote(10_000)["monthly_price_cents"], 600)
        self.assertEqual(self.app.subscriptions.quote(10_001)["monthly_price_cents"], 700)

        with self.assertRaisesRegex(Exception, "高级订阅"):
            self.app.channels.set_format_policy(
                channel["id"],
                "standard_card",
                enabled=True,
                owner_price_band="custom",
                platform_promo_enabled=False,
                custom_multiplier_bps=15000,
            )

        subscription = self.app.subscriptions.activate(
            channel_id=channel["id"],
            subscriber_count=10_001,
        )
        policy = self.app.channels.set_format_policy(
            channel["id"],
            "standard_card",
            enabled=True,
            owner_price_band="custom",
            platform_promo_enabled=False,
            custom_multiplier_bps=15000,
        )

        self.assertEqual(subscription["monthly_price_cents"], 700)
        self.assertEqual(policy["owner_price_band"], "custom")
        self.assertEqual(policy["platform_promo_enabled"], 0)

    def test_publisher_can_purchase_subscription_from_balance(self) -> None:
        channel = self.bind_channel()
        self.app.ledger.manual_topup(20001, money_to_cents("10"), display_name="频道主")

        subscription = self.app.subscriptions.purchase(
            channel_id=channel["id"],
            subscriber_count=10_001,
            months=1,
        )

        with self.app.db.transaction() as conn:
            publisher = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            platform = conn.execute("SELECT * FROM accounts WHERE id = 'platform'").fetchone()
            ledger_types = [
                row["type"]
                for row in conn.execute("SELECT type FROM ledger_transactions ORDER BY created_at").fetchall()
            ]
        self.assertEqual(subscription["monthly_price_cents"], 700)
        self.assertEqual(subscription["charged_cents"], 700)
        self.assertEqual(publisher["available_balance_cents"], 300)
        self.assertEqual(publisher["spent_balance_cents"], 700)
        self.assertEqual(platform["available_balance_cents"], 700)
        self.assertIn("publisher_subscription_charged", ledger_types)
        self.assertIn("publisher_subscription_revenue", ledger_types)

    def test_purchase_subscription_without_balance_fails(self) -> None:
        channel = self.bind_channel()

        with self.assertRaisesRegex(Exception, "余额不足"):
            self.app.subscriptions.purchase(
                channel_id=channel["id"],
                subscriber_count=10_001,
                months=1,
            )
        self.assertIsNone(self.app.subscriptions.get_active(channel["id"]))

    def test_stars_topup_invoice_pre_checkout_and_successful_payment(self) -> None:
        invoice = self.app.stars_payments.create_balance_topup_invoice(
            telegram_user_id=10001,
            stars_amount=500,
            display_name="广告主",
        )
        payload = invoice["intent"]["payload"]

        approved = self.app.update_handler.handle(
            {
                "pre_checkout_query": {
                    "id": "pcq_1",
                    "from": {"id": 10001},
                    "currency": "XTR",
                    "total_amount": 500,
                    "invoice_payload": payload,
                }
            }
        )
        paid = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 9,
                    "from": {"id": 10001},
                    "chat": {"id": 10001},
                    "successful_payment": {
                        "currency": "XTR",
                        "total_amount": 500,
                        "invoice_payload": payload,
                        "telegram_payment_charge_id": "tg_charge_topup_1",
                    },
                }
            }
        )
        duplicate = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 10,
                    "from": {"id": 10001},
                    "chat": {"id": 10001},
                    "successful_payment": {
                        "currency": "XTR",
                        "total_amount": 500,
                        "invoice_payload": payload,
                        "telegram_payment_charge_id": "tg_charge_topup_1",
                    },
                }
            }
        )

        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            intent = conn.execute("SELECT * FROM stars_payment_intents WHERE payload = ?", (payload,)).fetchone()
            topup_count = conn.execute("SELECT COUNT(*) AS n FROM ledger_transactions WHERE type = 'stars_topup'").fetchone()["n"]

        self.assertEqual(approved["type"], "pre_checkout_approved")
        self.assertTrue(self.gateway.pre_checkout_answers[-1]["ok"])
        self.assertEqual(paid["type"], "stars_balance_topup")
        self.assertEqual(duplicate["type"], "stars_payment_already_fulfilled")
        self.assertEqual(advertiser["available_balance_cents"], 500)
        self.assertEqual(intent["status"], "fulfilled")
        self.assertEqual(topup_count, 1)

    def test_stars_pre_checkout_rejects_wrong_amount(self) -> None:
        invoice = self.app.stars_payments.create_balance_topup_invoice(
            telegram_user_id=10001,
            stars_amount=500,
        )

        rejected = self.app.update_handler.handle(
            {
                "pre_checkout_query": {
                    "id": "pcq_bad",
                    "from": {"id": 10001},
                    "currency": "XTR",
                    "total_amount": 499,
                    "invoice_payload": invoice["intent"]["payload"],
                }
            }
        )

        self.assertEqual(rejected["type"], "pre_checkout_rejected")
        self.assertFalse(self.gateway.pre_checkout_answers[-1]["ok"])
        self.assertIn("金额不匹配", self.gateway.pre_checkout_answers[-1]["error_message"])

    def test_stars_publisher_subscription_successful_payment(self) -> None:
        channel = self.bind_channel()
        invoice = self.app.stars_payments.create_publisher_subscription_invoice(
            channel_id=channel["id"],
            subscriber_count=10_001,
            months=1,
        )
        payload = invoice["intent"]["payload"]

        self.app.update_handler.handle(
            {
                "pre_checkout_query": {
                    "id": "pcq_pub",
                    "from": {"id": 20001},
                    "currency": "XTR",
                    "total_amount": 700,
                    "invoice_payload": payload,
                }
            }
        )
        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 11,
                    "from": {"id": 20001},
                    "chat": {"id": 20001},
                    "successful_payment": {
                        "currency": "XTR",
                        "total_amount": 700,
                        "invoice_payload": payload,
                        "telegram_payment_charge_id": "tg_charge_pub_1",
                    },
                }
            }
        )

        active = self.app.subscriptions.get_active(channel["id"])
        with self.app.db.transaction() as conn:
            publisher = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            platform = conn.execute("SELECT * FROM accounts WHERE id = 'platform'").fetchone()
            ledger_types = [row["type"] for row in conn.execute("SELECT type FROM ledger_transactions").fetchall()]

        self.assertEqual(result["type"], "stars_publisher_subscription")
        self.assertIsNotNone(active)
        self.assertEqual(active["monthly_price_cents"], 700)
        self.assertEqual(publisher["available_balance_cents"], 0)
        self.assertEqual(publisher["spent_balance_cents"], 700)
        self.assertEqual(platform["available_balance_cents"], 700)
        self.assertIn("stars_topup", ledger_types)
        self.assertIn("publisher_subscription_charged", ledger_types)

    def test_stars_advertiser_plan_successful_payment(self) -> None:
        invoice = self.app.stars_payments.create_advertiser_subscription_invoice(
            advertiser_telegram_user_id=10001,
            plan="pro",
            months=1,
        )
        payload = invoice["intent"]["payload"]

        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 12,
                    "from": {"id": 10001},
                    "chat": {"id": 10001},
                    "successful_payment": {
                        "currency": "XTR",
                        "total_amount": 1900,
                        "invoice_payload": payload,
                        "telegram_payment_charge_id": "tg_charge_adsub_1",
                    },
                }
            }
        )
        status = self.app.advertiser_subscriptions.status(10001)

        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            platform = conn.execute("SELECT * FROM accounts WHERE id = 'platform'").fetchone()

        self.assertEqual(result["type"], "stars_advertiser_subscription")
        self.assertEqual(status["entitlements"]["plan"], "pro")
        self.assertEqual(advertiser["available_balance_cents"], 0)
        self.assertEqual(advertiser["spent_balance_cents"], 1900)
        self.assertEqual(platform["available_balance_cents"], 1900)

    def test_non_premium_without_promo_pays_service_fee_if_policy_exists(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")
        with self.app.db.transaction() as conn:
            conn.execute(
                """
                UPDATE channel_ad_format_policies
                SET platform_promo_enabled = 0
                WHERE channel_id = ? AND format_type = 'standard_card'
                """,
                (channel["id"],),
            )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            text="这是一条插播广告",
            target_url="https://example.com",
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()

        with self.app.db.transaction() as conn:
            publisher = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            platform = conn.execute("SELECT * FROM accounts WHERE id = 'platform'").fetchone()
        self.assertEqual(publisher["pending_earnings_cents"], 950)
        self.assertEqual(platform["available_balance_cents"], 50)

    def test_advertiser_discovery_saved_channels_and_alerts(self) -> None:
        channel = self.bind_channel()
        self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="software",
            median_24h_views=30_000,
            subscribers=60_000,
            light_unique_clickers_30d=300,
            repeat_purchase_count=2,
            risk_level="normal",
        )

        discovered = self.app.advertisers.discover_channels(
            category="software",
            min_score=70,
            max_risk_level="normal",
            slot_type="standard_card",
        )
        saved = self.app.advertisers.save_channel(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
            note="优先测试",
        )
        saved_list = self.app.advertisers.list_saved_channels(10001)
        self.topup_advertiser("20")
        subscription = self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )
        rule = self.app.advertisers.create_alert_rule(
            advertiser_telegram_user_id=10001,
            category="software",
            min_score=70,
            max_risk_level="normal",
            slot_type="standard_card",
        )
        events = self.app.advertisers.scan_alerts(10001)
        repeated_events = self.app.advertisers.scan_alerts(10001)
        listed_events = self.app.advertisers.list_alert_events(10001)

        self.assertEqual(discovered[0]["channel_id"], channel["id"])
        self.assertEqual(saved["channel_id"], channel["id"])
        self.assertEqual(saved_list[0]["note"], "优先测试")
        self.assertEqual(subscription["plan"], "pro")
        self.assertEqual(rule["category"], "software")
        self.assertEqual(len(events), 1)
        self.assertEqual(len(repeated_events), 1)
        self.assertEqual(len(listed_events), 1)
        self.assertEqual(listed_events[0]["channel_id"], channel["id"])

    def test_advertiser_subscription_limits_and_expires(self) -> None:
        with self.assertRaisesRegex(Exception, "高级服务"):
            self.app.advertisers.create_alert_rule(
                advertiser_telegram_user_id=10001,
                category="software",
            )

        self.topup_advertiser("20")
        subscription = self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )
        status = self.app.advertiser_subscriptions.status(10001)
        rule = self.app.advertisers.create_alert_rule(
            advertiser_telegram_user_id=10001,
            category="software",
        )

        self.assertEqual(subscription["charged_cents"], 1900)
        self.assertEqual(status["entitlements"]["plan"], "pro")
        self.assertEqual(rule["category"], "software")

        with self.app.db.transaction() as conn:
            conn.execute(
                """
                UPDATE advertiser_subscriptions
                SET expires_at = '2000-01-01T00:00:00+00:00'
                WHERE id = ?
                """,
                (subscription["id"],),
            )

        expired_status = self.app.advertiser_subscriptions.status(10001)
        self.assertEqual(expired_status["entitlements"]["plan"], "free")
        self.assertIsNone(expired_status["subscription"])
        channel = self.bind_channel()
        self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="software",
            median_24h_views=30_000,
            subscribers=60_000,
            light_unique_clickers_30d=300,
            risk_level="normal",
        )
        self.assertEqual(self.app.advertisers.scan_alerts(), [])
        with self.assertRaisesRegex(Exception, "高级服务"):
            self.app.advertisers.list_alert_events(10001)

    def test_discovery_free_limit_and_paid_limit(self) -> None:
        for index in range(7):
            channel = self.app.channels.bind_channel(
                telegram_chat_id=-100900 - index,
                title=f"发现频道{index}",
                username=f"discover_channel_{index}",
                owner_telegram_user_id=21000 + index,
                owner_display_name=f"频道主{index}",
            )
            self.app.pricing.assess_channel(
                channel_id=channel["id"],
                category="software",
                median_24h_views=10_000 + index,
                subscribers=20_000 + index,
                light_unique_clickers_30d=100,
                risk_level="normal",
            )

        free_results = self.app.advertisers.discover_channels(
            category="software",
            max_risk_level="normal",
            limit=10,
        )
        self.topup_advertiser("20")
        self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )
        paid_results = self.app.advertisers.discover_channels(
            advertiser_telegram_user_id=10001,
            category="software",
            max_risk_level="normal",
            limit=10,
        )

        self.assertEqual(len(free_results), 5)
        self.assertEqual(len(paid_results), 7)

    def test_advertiser_report_and_batch_orders(self) -> None:
        channel_a = self.bind_channel()
        channel_b = self.app.channels.bind_channel(
            telegram_chat_id=-100124,
            title="测试频道B",
            username="test_channel_b",
            owner_telegram_user_id=20002,
            owner_display_name="频道主B",
        )
        self.topup_advertiser("50")
        self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )

        batch = self.app.advertisers.create_batch_orders(
            advertiser_telegram_user_id=10001,
            channel_tokens=[channel_a["ref_token"], channel_b["ref_token"]],
            slot_type="standard_card",
            text="批量插播广告",
            target_url="https://batch.example",
            budget_cents=money_to_cents("10"),
        )
        self.assertEqual(batch["created_count"], 2)
        self.assertEqual(batch["failed_count"], 0)
        for item in batch["results"]:
            self.app.orders.approve_order(item["order_id"])
        self.app.fulfillment.dispatch_due(limit=10)

        with self.app.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries LIMIT 1").fetchone()
        self.confirm_timezone(333, display_name="点击用户")
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 2,
                    "from": {"id": 333, "first_name": "点击用户"},
                    "chat": {"id": 333},
                    "text": f"/start ad_{delivery['id']}",
                }
            }
        )
        report = self.app.advertisers.report(10001)

        self.assertEqual(report["orders_count"], 2)
        self.assertEqual(report["spent_cents"], 2000)
        self.assertEqual(report["sent_count"], 2)
        self.assertEqual(report["bot_starts"], 1)
        self.assertEqual(len(report["by_channel"]), 2)


if __name__ == "__main__":
    unittest.main()
