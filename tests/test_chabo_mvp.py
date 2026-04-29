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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chabo.app import create_app
from chabo.config import Settings
from chabo.money import money_to_cents
from chabo.polling import PollingRunner
from chabo.services import InvalidState, NotFound
from chabo.telegram import TelegramError
from chabo.web import make_server

from _fixtures import FakeGateway


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
        self.assertIn("展示：未选择", landing["text"])
        self.assertIn("下一步：选择展示设置", landing["text"])
        button_texts = [button["text"] for row in landing["inline_keyboard"] for button in row]
        self.assertIn("🧩 展示设置", button_texts)
        self.assertIn("⏱ 发布设置", button_texts)
        self.assertIn("📁 广告素材", button_texts)
        self.assertIn("✅ 费用确认", button_texts)
        self.assertNotIn("🗂 广告库", button_texts)
        self.assertNotIn("💰 广告钱包", button_texts)

        with self.app.db.transaction() as conn:
            session = conn.execute("SELECT * FROM advertiser_sessions").fetchone()
        self.assertEqual(session["ref_channel_id"], channel["id"])

    def test_channel_sales_entry_uses_placement_configurator(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("30")

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
                    "id": "cb_place_display",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:display",
                }
            }
        )
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

        self.assertEqual(display["type"], "callback_placement_display")
        self.assertEqual(slot["type"], "callback_placement_slot")
        self.assertEqual(pinned["type"], "callback_placement_pin")
        self.assertEqual(weekly["type"], "callback_placement_period")
        self.assertIn("展示：标准插播 + 置顶", self.gateway.private_messages[-1]["text"])
        self.assertIn("发布：7 天循环", self.gateway.private_messages[-1]["text"])
        self.assertIn("费用：预计 USD 126.00", self.gateway.private_messages[-1]["text"])

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
        creative = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 51,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "这是通过投放配置器创建的标准插播广告",
                }
            }
        )
        url = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 52,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "https://placement.example",
                }
            }
        )
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
        self.assertEqual(creative["type"], "placement_creative_saved")
        self.assertEqual(url["type"], "placement_url_saved")
        self.assertEqual(submit["type"], "callback_placement_order_created")
        self.assertEqual(order["status"], "approved")
        self.assertEqual(order["budget_cents"], 1000)
        self.assertEqual(order["unit_price_cents"], 1000)
        self.assertEqual(creative_row["target_url"], "https://placement.example")
        self.assertIsNone(state)

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

        self.assertEqual(first_start["type"], "timezone_prompt")
        self.assertIn("时区确认", self.gateway.private_messages[0]["text"])
        self.assertEqual(timezone_ok["type"], "callback_timezone_confirmed")
        self.assertEqual(start["type"], "main_menu")
        self.assertEqual(self.gateway.text_edits[-1]["inline_keyboard"][0][0]["text"], "➕ 添加频道")
        self.assertNotIn("我是广告主", self.gateway.text_edits[-1]["text"])
        self.assertNotIn("我是频道主", self.gateway.text_edits[-1]["text"])
        self.assertEqual(len(self.gateway.text_edits[-1]["inline_keyboard"][1]), 2)
        self.assertTrue(any(message["inline_keyboard"] and message["inline_keyboard"][0][0]["text"] == "➕ 添加频道" for message in self.gateway.private_messages))
        self.assertEqual(advertiser_menu["type"], "callback_advertiser_menu")
        self.assertEqual(balance["type"], "callback_advertiser_balance")
        self.assertEqual(library["type"], "callback_advertiser_library")
        self.assertEqual(len(self.gateway.callback_answers), 4)
        self.assertIn("广告库", self.gateway.private_messages[-1]["text"])

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

    def test_start_recognizes_publisher_owned_channels(self) -> None:
        channel = self.bind_channel()
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
        self.assertIn("1 个频道资产", self.gateway.private_messages[-1]["text"])
        self.assertEqual(self.gateway.private_messages[-1]["inline_keyboard"][0][0]["text"], f"📺 {channel['title']}")

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
        self.assertTrue(any(message["chat_id"] == "20002" and "频道已加入插播" in message["text"] for message in self.gateway.private_messages))

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

        self.confirm_timezone(333, display_name="点击用户")
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
        last_message = self.gateway.private_messages[-1]
        self.assertIn("完整广告详情", last_message["text"])
        keyboard_urls = [b.get("url") for row in last_message["inline_keyboard"] for b in row]
        self.assertIn("https://light.example", keyboard_urls)

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
        self.assertGreaterEqual(len(order_detail["order"]["时间线"]), 1)
        # The audit log for the operator approve action should be on the timeline
        self.assertTrue(
            any(event["title"] == "order_approved" for event in order_detail["order"]["时间线"])
        )
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
        self.assertIn("频道已接入", self.gateway.private_messages[-3]["text"])
        self.assertEqual(formats["type"], "callback_publisher_formats")
        self.assertEqual(toggled["type"], "callback_publisher_format_toggled")
        self.assertEqual(strong_policy["enabled"], 0)
        self.assertIn("定制插播", self.gateway.private_messages[-1]["text"])
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

        for user_id in [333, 444]:
            self.confirm_timezone(user_id, display_name="点击用户")
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
        keyboard = self.gateway.sent_ads[0]["inline_keyboard"]
        self.assertEqual(len(keyboard), 2)
        top_row_texts = [b["text"] for b in keyboard[0]]
        self.assertIn("📣 频道招商", top_row_texts)
        self.assertIn("🔍 查看详情", top_row_texts)
        sales_button = next(b for b in keyboard[0] if b["text"] == "📣 频道招商")
        detail_button = next(b for b in keyboard[0] if b["text"] == "🔍 查看详情")
        self.assertIn("ch_", sales_button["url"])
        self.assertIn("ad_del_", detail_button["url"])
        self.assertEqual(keyboard[1][0]["text"], "查看详情")

        with self.app.db.transaction() as conn:
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            publisher = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            platform = conn.execute("SELECT * FROM accounts WHERE id = 'platform'").fetchone()
            saved_order = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (order["id"],)).fetchone()
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
            ledger_count = conn.execute("SELECT COUNT(*) AS n FROM ledger_transactions").fetchone()["n"]

        self.assertEqual(advertiser["available_balance_cents"], 0)
        self.assertEqual(advertiser["reserved_balance_cents"], 0)
        self.assertEqual(advertiser["spent_balance_cents"], 1000)
        self.assertEqual(publisher["pending_earnings_cents"], 1000)
        self.assertEqual(platform["available_balance_cents"], 0)
        self.assertEqual(saved_order["status"], "budget_exhausted")
        self.assertEqual(delivery["status"], "sent")
        self.assertGreaterEqual(ledger_count, 4)

    def test_ad_deep_link_records_bot_start_metric(self) -> None:
        _, order = self.create_approved_order()
        self.app.fulfillment.dispatch_due()
        self.confirm_timezone(333, display_name="点击用户")
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
        last_message = self.gateway.private_messages[-1]
        keyboard_urls = [b.get("url") for row in last_message["inline_keyboard"] for b in row]
        self.assertIn("https://example.com", keyboard_urls)

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

    def test_create_batch_orders_with_material_id_reuses_creative(self) -> None:
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
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="共享素材",
            target_url="https://example.com/share",
        )

        batch = self.app.advertisers.create_batch_orders(
            advertiser_telegram_user_id=10001,
            channel_tokens=[channel_a["ref_token"], channel_b["ref_token"]],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.assertEqual(batch["created_count"], 2)
        with self.app.db.transaction() as conn:
            creative_ids = [
                row["creative_id"]
                for row in conn.execute(
                    "SELECT creative_id FROM ad_orders WHERE id IN (?, ?)",
                    (batch["results"][0]["order_id"], batch["results"][1]["order_id"]),
                ).fetchall()
            ]
        self.assertEqual(set(creative_ids), {material["id"]})

        # Library should still be a single material (no inline duplicates)
        items = self.app.materials.list_materials(advertiser_telegram_user_id=10001)
        self.assertEqual([m["id"] for m in items], [material["id"]])

    def test_create_batch_orders_requires_material_or_inline(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("50")
        self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )
        with self.assertRaises(InvalidState):
            self.app.advertisers.create_batch_orders(
                advertiser_telegram_user_id=10001,
                channel_tokens=[channel["ref_token"]],
                slot_type="standard_card",
                budget_cents=money_to_cents("5"),
            )

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


    # --------- Ad library / MaterialService ---------

    def test_material_library_creates_three_formats_with_ownership(self) -> None:
        light = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="light_tail",
            text="完整文字插播详情文案",
            target_url="https://example.com/detail",
            light_short_text="想投这里？",
            display_name="广告主",
        )
        std = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="标准插播文案 v1",
            target_url="https://example.com/std",
        )
        custom = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="strong_post",
            text="定制插播文案",
            target_url="https://example.com/strong",
            button_text="立即下载",
        )
        self.assertEqual(light["format_type"], "light_tail")
        self.assertEqual(light["light_short_text"], "想投这里？")
        self.assertIsNone(std["light_short_text"])
        self.assertEqual(custom["button_text"], "立即下载")

        listed = self.app.materials.list_materials(advertiser_telegram_user_id=10001)
        self.assertEqual(len(listed), 3)
        light_only = self.app.materials.list_materials(
            advertiser_telegram_user_id=10001, format_type="light_tail"
        )
        self.assertEqual([item["id"] for item in light_only], [light["id"]])

        # Foreign user cannot read another advertiser's material
        with self.assertRaises(NotFound):
            self.app.materials.get_material(
                std["id"], advertiser_telegram_user_id=99999
            )

    def test_material_library_validates_format_and_short_text(self) -> None:
        with self.assertRaises(InvalidState):
            self.app.materials.create_material(
                advertiser_telegram_user_id=10001,
                format_type="pin24h",
                text="不合法",
                target_url="https://example.com",
            )
        with self.assertRaises(InvalidState):
            self.app.materials.create_material(
                advertiser_telegram_user_id=10001,
                format_type="light_tail",
                text="缺少短入口",
                target_url="https://example.com",
            )
        with self.assertRaises(InvalidState):
            self.app.materials.create_material(
                advertiser_telegram_user_id=10001,
                format_type="light_tail",
                text="详情",
                target_url="https://example.com",
                light_short_text="超过十五个字的短入口测试一二三四五",
            )

    def test_create_order_reuses_library_material_and_blocks_archived(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="可复用的标准插播文案",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.assertEqual(order["creative_id"], material["id"])

        # Foreign advertiser cannot use someone else's material_id
        with self.assertRaises(NotFound):
            self.app.orders.create_order(
                advertiser_telegram_user_id=99999,
                channel_token=channel["ref_token"],
                slot_type="standard_card",
                material_id=material["id"],
                budget_cents=money_to_cents("10"),
            )

        # Archive the material — subsequent orders should be rejected
        self.app.materials.archive_material(
            material["id"], advertiser_telegram_user_id=10001
        )
        with self.assertRaises(InvalidState):
            self.app.orders.create_order(
                advertiser_telegram_user_id=10001,
                channel_token=channel["ref_token"],
                slot_type="standard_card",
                material_id=material["id"],
                budget_cents=money_to_cents("10"),
            )

        # Default list excludes archived; include_archived shows it again
        active = self.app.materials.list_materials(advertiser_telegram_user_id=10001)
        self.assertNotIn(material["id"], [m["id"] for m in active])
        with_archived = self.app.materials.list_materials(
            advertiser_telegram_user_id=10001, include_archived=True
        )
        self.assertIn(material["id"], [m["id"] for m in with_archived])

    def test_inline_create_order_populates_library(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("10")
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            text="inline 文案",
            target_url="https://example.com",
            budget_cents=money_to_cents("10"),
        )
        listed = self.app.materials.list_materials(advertiser_telegram_user_id=10001)
        self.assertEqual([m["id"] for m in listed], [order["creative_id"]])
        self.assertEqual(listed[0]["format_type"], "standard_card")

    def test_placement_creative_panel_filters_by_format_and_skips_archived(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        std = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="标准素材-A",
            target_url="https://example.com/a",
        )
        self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="strong_post",
            text="定制素材-不该出现",
            target_url="https://example.com/b",
        )
        std_archived = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="已归档-不该出现",
            target_url="https://example.com/c",
        )
        self.app.materials.archive_material(
            std_archived["id"], advertiser_telegram_user_id=10001
        )

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_lib_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": f"channel:order:{channel['id']}",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_lib_2",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:slot:standard_card",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_place_lib_3",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:creative",
                }
            }
        )

        with self.app.db.transaction() as conn:
            state = conn.execute(
                "SELECT * FROM bot_conversation_states WHERE chat_id = '10001'"
            ).fetchone()
        self.assertIsNotNone(state)
        payload = json.loads(state["payload_json"])
        self.assertEqual(payload["creative_ids"], [std["id"]])

    def test_placement_pick_then_submit_reuses_library_material(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("20")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="可复用的标准插播文案",
            target_url="https://example.com",
        )

        for cb_id, data in [
            ("cb_pick_1", f"channel:order:{channel['id']}"),
            ("cb_pick_2", "place:slot:standard_card"),
            ("cb_pick_3", "place:creative"),
            ("cb_pick_4", "place:pick:0"),
        ]:
            self.app.update_handler.handle(
                {
                    "callback_query": {
                        "id": cb_id,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "message": {"chat": {"id": 10001}},
                        "data": data,
                    }
                }
            )
        submit = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pick_submit",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:submit",
                }
            }
        )

        self.assertEqual(submit["type"], "callback_placement_order_created")
        with self.app.db.transaction() as conn:
            order = conn.execute(
                "SELECT * FROM ad_orders WHERE id = ?", (submit["order_id"],)
            ).fetchone()
        self.assertEqual(order["creative_id"], material["id"])

        # Library should still have only one material — pick reuses, not duplicates
        items = self.app.materials.list_materials(advertiser_telegram_user_id=10001)
        self.assertEqual([m["id"] for m in items], [material["id"]])

    def test_placement_archive_callback_archives_and_clears_selection(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        first = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="素材-A",
            target_url="https://example.com/a",
        )
        second = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="素材-B",
            target_url="https://example.com/b",
        )

        for cb_id, data in [
            ("cb_arch_1", f"channel:order:{channel['id']}"),
            ("cb_arch_2", "place:slot:standard_card"),
            ("cb_arch_3", "place:creative"),
        ]:
            self.app.update_handler.handle(
                {
                    "callback_query": {
                        "id": cb_id,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "message": {"chat": {"id": 10001}},
                        "data": data,
                    }
                }
            )

        # Whichever order the bot listed, pick index 0 (selection) and then archive index 0
        with self.app.db.transaction() as conn:
            state_before = conn.execute(
                "SELECT * FROM bot_conversation_states WHERE chat_id = '10001'"
            ).fetchone()
        ordered_ids = json.loads(state_before["payload_json"])["creative_ids"]
        self.assertEqual(set(ordered_ids), {first["id"], second["id"]})
        archive_target_id = ordered_ids[0]
        keep_id = ordered_ids[1]

        for cb_id, data in [
            ("cb_arch_pick", "place:pick:0"),
            ("cb_arch_back", "place:creative"),
            ("cb_arch_archive", "place:archive:0"),
        ]:
            self.app.update_handler.handle(
                {
                    "callback_query": {
                        "id": cb_id,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "message": {"chat": {"id": 10001}},
                        "data": data,
                    }
                }
            )

        active = self.app.materials.list_materials(advertiser_telegram_user_id=10001)
        self.assertEqual([m["id"] for m in active], [keep_id])
        archived = self.app.materials.get_material(archive_target_id)
        self.assertIsNotNone(archived["archived_at"])

        with self.app.db.transaction() as conn:
            state_after = conn.execute(
                "SELECT * FROM bot_conversation_states WHERE chat_id = '10001'"
            ).fetchone()
        payload_after = json.loads(state_after["payload_json"])
        self.assertNotIn("material_id", payload_after)
        self.assertEqual(payload_after["creative_ids"], [keep_id])

    def _last_user_facing_text(self) -> str:
        """Return the most recent text shown to the user (edits beat new sends)."""
        edits = list(self.gateway.text_edits)
        privs = list(self.gateway.private_messages)
        if not edits and not privs:
            raise AssertionError("no user-facing message recorded")
        if edits and not privs:
            return edits[-1]["text"]
        if privs and not edits:
            return privs[-1]["text"]
        return edits[-1]["text"]  # callback flows always end on an edit

    def _last_user_facing_keyboard(self) -> list[list[dict[str, str]]]:
        edits = list(self.gateway.text_edits)
        privs = list(self.gateway.private_messages)
        if edits:
            return edits[-1]["inline_keyboard"] or []
        if privs:
            return privs[-1]["inline_keyboard"] or []
        raise AssertionError("no user-facing keyboard recorded")

    def test_advertiser_orders_list_shows_detail_buttons_and_renders_per_order(self) -> None:
        _, order = self.create_approved_order()
        self.confirm_timezone(10001, display_name="广告主")

        list_result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_orders_list",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:orders",
                }
            }
        )
        self.assertTrue(list_result["handled"])
        list_text = self._last_user_facing_text()
        keyboard = self._last_user_facing_keyboard()
        all_buttons = [b for row in keyboard for b in row]
        detail_buttons = [b for b in all_buttons if b["callback_data"].startswith("advertiser:order:")]
        self.assertEqual(len(detail_buttons), 1)
        self.assertEqual(detail_buttons[0]["callback_data"], f"advertiser:order:{order['id']}")
        self.assertIn("①", list_text)

        detail_result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_order_detail",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}",
                }
            }
        )
        self.assertEqual(detail_result["type"], "callback_advertiser_order_detail")
        self.assertEqual(detail_result["order_id"], order["id"])
        detail_text = self._last_user_facing_text()
        self.assertIn(order["id"], detail_text)
        self.assertIn("测试频道", detail_text)
        self.assertIn("已审", detail_text)
        self.assertIn("发布记录", detail_text)

    def test_advertiser_order_detail_rejects_other_users_orders(self) -> None:
        _, order = self.create_approved_order()
        self.confirm_timezone(99999, display_name="他人")

        result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_order_steal",
                    "from": {"id": 99999, "first_name": "他人"},
                    "message": {"chat": {"id": 99999}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}",
                }
            }
        )
        self.assertEqual(result["type"], "callback_advertiser_order_detail")
        self.assertIn("不存在或不属于你", self._last_user_facing_text())

    def test_advertiser_order_detail_surfaces_failed_delivery_reason(self) -> None:
        self.gateway.fail_send = True
        _, order = self.create_approved_order()
        self.confirm_timezone(10001, display_name="广告主")
        self.app.fulfillment.dispatch_due()

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_order_failed_detail",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}",
                }
            }
        )
        detail_text = self._last_user_facing_text()
        self.assertIn("❌", detail_text)
        self.assertIn("原因", detail_text)

    def _seed_two_assessed_channels(self) -> tuple[dict, dict]:
        channel_a = self.bind_channel()
        channel_b = self.app.channels.bind_channel(
            telegram_chat_id=-100124,
            title="测试频道B",
            username="test_channel_b",
            owner_telegram_user_id=20002,
            owner_display_name="频道主B",
        )
        for ch in (channel_a, channel_b):
            self.app.pricing.assess_channel(
                channel_id=ch["id"],
                category="software",
                median_24h_views=20_000,
                subscribers=50_000,
                light_clicks_30d=180,
                light_unique_clickers_30d=120,
                repeat_purchase_count=2,
                dispute_count=0,
                risk_level="normal",
            )
            self.app.pricing.apply_quotes_to_rate_cards(ch["id"])
        return channel_a, channel_b

    def test_library_exposes_batch_button_per_material(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="批量待投素材",
            target_url="https://example.com",
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_lib",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:library",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        batch_buttons = [
            b for row in keyboard for b in row
            if b["callback_data"].startswith("advertiser:batch:start:")
        ]
        self.assertEqual(len(batch_buttons), 1)
        self.assertEqual(batch_buttons[0]["callback_data"], f"advertiser:batch:start:{material['id']}")

    def test_batch_flow_toggle_select_and_submit_creates_orders(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        # Standard_card list prices in our seed are ~USD 118; top up enough for two
        self.topup_advertiser("500")
        self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="批量素材文案",
            target_url="https://example.com/batch",
        )
        channel_a, channel_b = self._seed_two_assessed_channels()

        # Start batch
        start = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_batch_start",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:batch:start:{material['id']}",
                }
            }
        )
        self.assertEqual(start["type"], "callback_batch_start")
        body = self._last_user_facing_text()
        self.assertIn("批量投放", body)
        self.assertIn(channel_a["title"], body)
        self.assertIn(channel_b["title"], body)

        # Bump per-channel budget above the standard_card list price by sending a budget message
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_batch_budget_prompt",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:batch:budget",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 99,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "150",
                }
            }
        )

        # Toggle both channels
        for cb_id, ch in (("cb_t_a", channel_a), ("cb_t_b", channel_b)):
            self.app.update_handler.handle(
                {
                    "callback_query": {
                        "id": cb_id,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "message": {"chat": {"id": 10001}, "message_id": 1},
                        "data": f"advertiser:batch:toggle:{ch['id']}",
                    }
                }
            )

        # Confirm 2 selected & submit
        body_after = self._last_user_facing_text()
        self.assertIn("已选：2", body_after)
        submit = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_batch_submit",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:batch:submit",
                }
            }
        )
        self.assertEqual(submit["type"], "callback_batch_submitted")
        self.assertEqual(submit["created_count"], 2)
        self.assertEqual(submit["failed_count"], 0)

        # Both new orders use the same creative_id (the library material)
        with self.app.db.transaction() as conn:
            creative_ids = [
                row["creative_id"]
                for row in conn.execute(
                    "SELECT creative_id FROM ad_orders WHERE advertiser_account_id IN (SELECT id FROM accounts WHERE telegram_user_id = '10001')"
                ).fetchall()
            ]
        self.assertEqual(set(creative_ids), {material["id"]})

        # Conversation cleaned
        with self.app.db.transaction() as conn:
            state = conn.execute(
                "SELECT flow FROM bot_conversation_states WHERE chat_id = '10001'"
            ).fetchone()
        self.assertTrue(state is None or state["flow"] != "batch_orders")

    def test_batch_flow_blocks_archived_material(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="即将归档",
            target_url="https://example.com",
        )
        self.app.materials.archive_material(material["id"], advertiser_telegram_user_id=10001)
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_batch_arch",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:batch:start:{material['id']}",
                }
            }
        )
        self.assertIn("已归档", self._last_user_facing_text())

    def test_batch_flow_for_free_user_fails_with_friendly_message(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("100")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="想批量但没买套餐",
            target_url="https://example.com",
        )
        channel_a, channel_b = self._seed_two_assessed_channels()
        # Start
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_free_start",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:batch:start:{material['id']}",
                }
            }
        )
        # Toggle
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_free_toggle",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:batch:toggle:{channel_a['id']}",
                }
            }
        )
        # Submit — service should reject for missing batch_orders feature
        result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_free_submit",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:batch:submit",
                }
            }
        )
        self.assertEqual(result["type"], "callback_batch_failed")

    def test_advertiser_menu_exposes_plan_entry(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_menu_plan",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "role:advertiser",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        all_buttons = [b for row in keyboard for b in row]
        plan = [b for b in all_buttons if b["callback_data"] == "advertiser:plan"]
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["text"], "📦 我的套餐")

    def test_advertiser_plan_panel_for_free_user_shows_both_upgrade_paths(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_plan_free",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:plan",
                }
            }
        )
        body = self._last_user_facing_text()
        self.assertIn("Free", body)
        self.assertIn("Pro", body)
        self.assertIn("Enterprise", body)
        self.assertIn("当前", body)
        keyboard = self._last_user_facing_keyboard()
        upgrades = [
            b for row in keyboard for b in row
            if b["callback_data"].startswith("advertiser:plan:buy:")
        ]
        self.assertEqual(
            sorted(b["callback_data"].removeprefix("advertiser:plan:buy:") for b in upgrades),
            ["enterprise", "pro"],
        )

    def test_advertiser_plan_panel_for_pro_user_hides_pro_upgrade_button(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("100")
        self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_plan_pro",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:plan",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        upgrade_callbacks = [
            b["callback_data"] for row in keyboard for b in row
            if b["callback_data"].startswith("advertiser:plan:buy:")
        ]
        self.assertEqual(upgrade_callbacks, ["advertiser:plan:buy:enterprise"])

    def test_advertiser_plan_buy_sends_stars_invoice_and_fulfills_on_payment(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        # Tap buy:pro → invoice sent to gateway
        result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_plan_buy",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:plan:buy:pro",
                }
            }
        )
        self.assertEqual(result["type"], "callback_advertiser_plan_invoice_sent")
        self.assertEqual(result["plan"], "pro")
        self.assertEqual(len(self.gateway.invoices), 1)
        invoice = self.gateway.invoices[0]
        self.assertEqual(invoice["currency"], "XTR")
        self.assertEqual(int(result["stars_amount"]), invoice["prices"][0]["amount"])

        # Simulate the user paying — fulfill_successful_payment should activate Pro
        payment = {
            "currency": "XTR",
            "total_amount": result["stars_amount"],
            "invoice_payload": invoice["payload"],
            "telegram_payment_charge_id": "charge_pro_test",
        }
        self.app.stars_payments.fulfill_successful_payment(
            payment, telegram_user_id=10001
        )
        status = self.app.advertiser_subscriptions.status(10001)
        self.assertEqual(status["entitlements"]["plan"], "pro")

    def test_advertiser_alerts_for_free_user_explains_pro_requirement(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_alerts_free",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:alerts",
                }
            }
        )
        body = self._last_user_facing_text()
        self.assertIn("Pro", body)
        self.assertIn("create-alert-rule", body)

    def test_advertiser_alerts_empty_for_pro_user_with_no_rules(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("100")
        self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_alerts_pro_empty",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:alerts",
                }
            }
        )
        body = self._last_user_facing_text()
        self.assertIn("暂无新提醒", body)

    def test_advertiser_alerts_lists_triggered_events_with_play_buttons(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("100")
        self.app.advertiser_subscriptions.purchase(
            advertiser_telegram_user_id=10001,
            plan="pro",
        )
        channel = self.bind_channel()
        self.app.pricing.assess_channel(
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
        self.app.pricing.apply_quotes_to_rate_cards(channel["id"])
        self.app.advertisers.create_alert_rule(
            advertiser_telegram_user_id=10001,
            category="software",
            min_score=0,
            max_risk_level="normal",
        )
        self.app.advertisers.scan_alerts(advertiser_telegram_user_id=10001)

        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_alerts_pro",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:alerts",
                }
            }
        )
        body = self._last_user_facing_text()
        self.assertIn(channel["title"], body)
        self.assertIn("🆕", body)
        keyboard = self._last_user_facing_keyboard()
        play_buttons = [
            b for row in keyboard for b in row
            if b["callback_data"].startswith("channel:order:")
        ]
        self.assertEqual(len(play_buttons), 1)
        self.assertEqual(play_buttons[0]["callback_data"], f"channel:order:{channel['id']}")

    def test_remove_saved_channel_returns_true_only_when_row_existed(self) -> None:
        channel = self.bind_channel()
        self.app.advertisers.save_channel(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
            note="先收藏",
        )
        self.assertTrue(self.app.advertisers.is_saved_channel(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
        ))
        first = self.app.advertisers.remove_saved_channel(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
        )
        second = self.app.advertisers.remove_saved_channel(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertFalse(self.app.advertisers.is_saved_channel(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
        ))

    def test_advertiser_menu_exposes_saved_entry_point(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_menu_saved",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "role:advertiser",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        all_buttons = [b for row in keyboard for b in row]
        saved = [b for b in all_buttons if b["callback_data"] == "advertiser:saved"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["text"], "⭐ 我的收藏")

    def test_saved_empty_state_guides_to_discover(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_saved_empty",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:saved",
                }
            }
        )
        body = self._last_user_facing_text()
        self.assertIn("还没有收藏", body)
        keyboard = self._last_user_facing_keyboard()
        callbacks = [b["callback_data"] for row in keyboard for b in row]
        self.assertIn("advertiser:discover", callbacks)

    def test_save_then_unsave_cycle_via_discover_and_saved_list(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.app.pricing.assess_channel(
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
        self.app.pricing.apply_quotes_to_rate_cards(channel["id"])

        # Discover page exposes ⭐ buttons
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_disc_for_save",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:discover",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        save_buttons = [
            b for row in keyboard for b in row
            if b["callback_data"].startswith("advertiser:save:disc:")
        ]
        self.assertEqual(len(save_buttons), 1)
        self.assertTrue(save_buttons[0]["text"].startswith("⭐"))

        # Tap save → channel becomes saved
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_save",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:save:disc:{channel['id']}",
                }
            }
        )
        self.assertTrue(self.app.advertisers.is_saved_channel(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
        ))
        # Discover refresh shows the 🌟 marker
        keyboard_after = self._last_user_facing_keyboard()
        save_after = [
            b for row in keyboard_after for b in row
            if b["callback_data"].startswith("advertiser:save:disc:")
        ]
        self.assertTrue(save_after[0]["text"].startswith("🌟"))

        # Saved list page lists the channel
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_saved_list",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:saved",
                }
            }
        )
        body = self._last_user_facing_text()
        self.assertIn(channel["title"], body)

        # Tap ❌ from saved list → unsave
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_unsave",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:save:saved:{channel['id']}",
                }
            }
        )
        self.assertFalse(self.app.advertisers.is_saved_channel(
            advertiser_telegram_user_id=10001,
            channel_id=channel["id"],
        ))
        body_after = self._last_user_facing_text()
        self.assertIn("还没有收藏", body_after)

    def test_advertiser_menu_exposes_discover_entry_point(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_menu",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "role:advertiser",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        all_buttons = [b for row in keyboard for b in row]
        discover = [b for b in all_buttons if b["callback_data"] == "advertiser:discover"]
        self.assertEqual(len(discover), 1)
        self.assertEqual(discover[0]["text"], "🔍 找频道")

    def test_advertiser_discover_lists_assessed_channels_with_play_buttons(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.app.pricing.assess_channel(
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
        self.app.pricing.apply_quotes_to_rate_cards(channel["id"])

        result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_discover",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:discover",
                }
            }
        )
        self.assertEqual(result["type"], "callback_advertiser_discover")
        body = self._last_user_facing_text()
        self.assertIn("找频道", body)
        self.assertIn(channel["title"], body)
        self.assertIn("软件", body)
        self.assertIn("订阅", body)

        keyboard = self._last_user_facing_keyboard()
        play_buttons = [
            b for row in keyboard for b in row
            if b["callback_data"].startswith("channel:order:")
        ]
        self.assertEqual(len(play_buttons), 1)
        self.assertEqual(play_buttons[0]["callback_data"], f"channel:order:{channel['id']}")

    def test_advertiser_discover_empty_state_guides_user(self) -> None:
        # No channels assessed yet
        self.confirm_timezone(10001, display_name="广告主")
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_discover_empty",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:discover",
                }
            }
        )
        body = self._last_user_facing_text()
        self.assertIn("暂无", body)
        self.assertIn("频道招商", body)  # nudge to alternate entry point

    def test_advertiser_discover_play_button_starts_placement_flow(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.app.pricing.assess_channel(
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
        self.app.pricing.apply_quotes_to_rate_cards(channel["id"])

        # Tap discover → tap ▶
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_disc_open",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:discover",
                }
            }
        )
        play = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_disc_play",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"channel:order:{channel['id']}",
                }
            }
        )
        self.assertEqual(play["type"], "callback_order_flow_started")
        # placement_config conversation should be set up for this channel
        with self.app.db.transaction() as conn:
            state = conn.execute(
                "SELECT * FROM bot_conversation_states WHERE chat_id = '10001'"
            ).fetchone()
        self.assertIsNotNone(state)
        self.assertEqual(state["flow"], "placement_config")
        payload = json.loads(state["payload_json"])
        self.assertEqual(payload["channel_id"], channel["id"])

    def test_library_create_button_walks_standard_format_end_to_end(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")

        # Library page exposes ➕ 新建素材
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_lib_open_new",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:library",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        all_buttons = [b for row in keyboard for b in row]
        self.assertTrue(
            any(b["callback_data"] == "advertiser:material:new" for b in all_buttons),
            "Library should expose ➕ 新建素材",
        )

        # Pick format → standard_card
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pick_format",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:material:new",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pick_standard",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:material:new:standard_card",
                }
            }
        )
        self.assertIn("请发送广告文案", self._last_user_facing_text())

        # Walk text → url
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 11,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "全新独立创建的标准插播文案",
                }
            }
        )
        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 12,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "https://example.com/standalone",
                }
            }
        )
        self.assertEqual(result["type"], "material_create_saved")

        # Material is in the library
        items = self.app.materials.list_materials(advertiser_telegram_user_id=10001)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "全新独立创建的标准插播文案")
        self.assertEqual(items[0]["target_url"], "https://example.com/standalone")
        self.assertEqual(items[0]["format_type"], "standard_card")

        # Conversation state cleaned up
        with self.app.db.transaction() as conn:
            state_row = conn.execute(
                "SELECT flow FROM bot_conversation_states WHERE chat_id = '10001'"
            ).fetchone()
        self.assertTrue(state_row is None or state_row["flow"] != "material_create")

    def test_library_create_light_tail_collects_short_then_detail_then_url(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        for cb_id, data in [
            ("cb_lt_picker", "advertiser:material:new"),
            ("cb_lt_format", "advertiser:material:new:light_tail"),
        ]:
            self.app.update_handler.handle(
                {
                    "callback_query": {
                        "id": cb_id,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "message": {"chat": {"id": 10001}, "message_id": 1},
                        "data": data,
                    }
                }
            )

        # Short entry too long → rejection, conversation stays at light_short_text
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 21,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "一" * 16,
                }
            }
        )
        # Errors go through send_private_message — assert directly against that channel
        self.assertIn("2-15 个字", self.gateway.private_messages[-1]["text"])

        # Within bounds
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 22,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "想看广告？",
                }
            }
        )
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 23,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "完整文字插播详情文案，应该够长。",
                }
            }
        )
        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 24,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "https://example.com/light",
                }
            }
        )
        self.assertEqual(result["type"], "material_create_saved")

        items = self.app.materials.list_materials(advertiser_telegram_user_id=10001)
        self.assertEqual(items[0]["format_type"], "light_tail")
        self.assertEqual(items[0]["light_short_text"], "想看广告？")
        self.assertEqual(items[0]["button_text"], "想看广告？")

    def test_library_create_url_must_be_http(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        for cb_id, data in [
            ("cb_url_picker", "advertiser:material:new"),
            ("cb_url_std", "advertiser:material:new:standard_card"),
        ]:
            self.app.update_handler.handle(
                {
                    "callback_query": {
                        "id": cb_id,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "message": {"chat": {"id": 10001}, "message_id": 1},
                        "data": data,
                    }
                }
            )
        self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 31,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "标准卡片正文文案",
                }
            }
        )
        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 32,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "ftp://example.com/wrong",
                }
            }
        )
        self.assertEqual(result["type"], "material_create_invalid_url")
        self.assertEqual(self.app.materials.list_materials(advertiser_telegram_user_id=10001), [])

    def test_material_edit_text_via_bot_updates_creative_and_renders_panel(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="原始文案",
            target_url="https://example.com/orig",
        )

        # Library page → tap ✏️
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_lib_open",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": "advertiser:library",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        edit_buttons = [
            b for row in keyboard for b in row
            if b["callback_data"].startswith("advertiser:material:edit:")
        ]
        self.assertEqual(len(edit_buttons), 1)
        self.assertEqual(edit_buttons[0]["callback_data"], f"advertiser:material:edit:{material['id']}")

        # Edit panel
        panel = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_edit_panel",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:material:edit:{material['id']}",
                }
            }
        )
        self.assertEqual(panel["type"], "callback_material_edit_panel")
        panel_text = self._last_user_facing_text()
        self.assertIn("编辑素材", panel_text)
        self.assertIn("原始文案", panel_text)

        # Tap 文案
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_edit_field_text",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:material:field:{material['id']}:text",
                }
            }
        )
        self.assertIn("请发送新的广告文案", self._last_user_facing_text())

        # Send replacement text
        send_result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 99,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "全新升级文案",
                }
            }
        )
        self.assertEqual(send_result["type"], "material_edit_saved")

        refreshed = self.app.materials.get_material(material["id"])
        self.assertEqual(refreshed["text"], "全新升级文案")
        self.assertEqual(refreshed["target_url"], "https://example.com/orig")
        self.assertNotEqual(refreshed["content_hash"], material["content_hash"])

    def test_material_edit_does_not_alter_existing_order_snapshot(self) -> None:
        channel, order = self.create_approved_order()
        # The order's creative came from inline create_order; locate it
        with self.app.db.transaction() as conn:
            row = conn.execute(
                "SELECT creative_id FROM ad_orders WHERE id = ?", (order["id"],)
            ).fetchone()
            material_id = row["creative_id"]
            snapshot_before = conn.execute(
                "SELECT payload_json FROM evidence_snapshots WHERE order_id = ? AND snapshot_type = 'creative'",
                (order["id"],),
            ).fetchone()["payload_json"]

        self.app.materials.update_material(
            material_id,
            advertiser_telegram_user_id=10001,
            text="修改后的文案",
        )

        with self.app.db.transaction() as conn:
            snapshot_after = conn.execute(
                "SELECT payload_json FROM evidence_snapshots WHERE order_id = ? AND snapshot_type = 'creative'",
                (order["id"],),
            ).fetchone()["payload_json"]
        self.assertEqual(snapshot_before, snapshot_after)
        refreshed = self.app.materials.get_material(material_id)
        self.assertEqual(refreshed["text"], "修改后的文案")

    def test_material_edit_blocks_archived_material(self) -> None:
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="将被归档",
            target_url="https://example.com/x",
        )
        self.app.materials.archive_material(material["id"], advertiser_telegram_user_id=10001)
        with self.assertRaises(InvalidState):
            self.app.materials.update_material(
                material["id"],
                advertiser_telegram_user_id=10001,
                text="不能改",
            )

    def test_material_edit_rejects_other_users_material(self) -> None:
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="A 的素材",
            target_url="https://example.com/a",
        )
        with self.assertRaises(NotFound):
            self.app.materials.update_material(
                material["id"],
                advertiser_telegram_user_id=99999,
                text="B 想偷改",
            )
        unchanged = self.app.materials.get_material(material["id"])
        self.assertEqual(unchanged["text"], "A 的素材")

    def test_material_edit_light_short_text_validates_length(self) -> None:
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="light_tail",
            text="文字插播完整文案",
            target_url="https://example.com",
            light_short_text="想投广告？",
        )
        with self.assertRaises(InvalidState):
            self.app.materials.update_material(
                material["id"],
                advertiser_telegram_user_id=10001,
                light_short_text="x",
            )
        with self.assertRaises(InvalidState):
            self.app.materials.update_material(
                material["id"],
                advertiser_telegram_user_id=10001,
                light_short_text="一" * 16,
            )
        # within bounds OK
        self.app.materials.update_material(
            material["id"],
            advertiser_telegram_user_id=10001,
            light_short_text="新短入口文案",
        )
        refreshed = self.app.materials.get_material(material["id"])
        self.assertEqual(refreshed["light_short_text"], "新短入口文案")

    def test_advertiser_dispute_button_appears_after_a_delivery_ships(self) -> None:
        _, order = self.create_approved_order()
        self.confirm_timezone(10001, display_name="广告主")

        # Before dispatch: no sent delivery → no 🚩 button
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_pre",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        dispute_buttons = [
            b for row in keyboard for b in row
            if b["callback_data"].endswith(":dispute")
        ]
        self.assertEqual(dispute_buttons, [])

        # Dispatch → delivery becomes sent → button appears
        self.app.fulfillment.dispatch_due()
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_post",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}",
                }
            }
        )
        keyboard_after = self._last_user_facing_keyboard()
        dispute_buttons_after = [
            b for row in keyboard_after for b in row
            if b["callback_data"].endswith(":dispute")
        ]
        self.assertEqual(len(dispute_buttons_after), 1)
        self.assertEqual(dispute_buttons_after[0]["callback_data"], f"advertiser:order:{order['id']}:dispute")

    def test_advertiser_dispute_full_flow_marks_delivery_disputed(self) -> None:
        _, order = self.create_approved_order()
        self.confirm_timezone(10001, display_name="广告主")
        self.app.fulfillment.dispatch_due()

        # Tap 🚩 → prompt
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_dispute_start",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}:dispute",
                }
            }
        )
        self.assertIn("申诉原因", self._last_user_facing_text())

        # Send reason
        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 99,
                    "from": {"id": 10001, "first_name": "广告主"},
                    "chat": {"id": 10001},
                    "text": "频道主提前删除了广告，没有完成承诺的曝光时长。",
                }
            }
        )
        self.assertEqual(result["type"], "dispute_opened")
        self.assertEqual(result["order_id"], order["id"])
        dispute_id = result["dispute_id"]

        with self.app.db.transaction() as conn:
            dispute = conn.execute(
                "SELECT * FROM disputes WHERE id = ?", (dispute_id,)
            ).fetchone()
            delivery = conn.execute(
                "SELECT status FROM deliveries WHERE order_id = ? ORDER BY scheduled_at DESC LIMIT 1",
                (order["id"],),
            ).fetchone()
        self.assertEqual(dispute["status"], "open")
        self.assertEqual(delivery["status"], "disputed")

        # Conversation cleaned up
        with self.app.db.transaction() as conn:
            state = conn.execute(
                "SELECT flow FROM bot_conversation_states WHERE chat_id = '10001'"
            ).fetchone()
        self.assertTrue(state is None or state["flow"] != "dispute_open")

    def test_advertiser_dispute_blocks_when_already_open(self) -> None:
        _, order = self.create_approved_order()
        self.confirm_timezone(10001, display_name="广告主")
        self.app.fulfillment.dispatch_due()
        self.app.disputes.advertiser_open_dispute(
            advertiser_telegram_user_id=10001,
            order_id=order["id"],
            reason="第一次申诉",
        )
        # Detail page no longer offers the 🚩 button
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_post_disp",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}",
                }
            }
        )
        keyboard = self._last_user_facing_keyboard()
        dispute_buttons = [
            b for row in keyboard for b in row
            if b["callback_data"].endswith(":dispute")
        ]
        self.assertEqual(dispute_buttons, [])

        # Direct service call also rejects
        with self.assertRaises(InvalidState):
            self.app.disputes.advertiser_open_dispute(
                advertiser_telegram_user_id=10001,
                order_id=order["id"],
                reason="想再开一次",
            )

    def test_advertiser_dispute_rejects_other_users_orders(self) -> None:
        _, order = self.create_approved_order()
        self.app.fulfillment.dispatch_due()
        with self.assertRaises(NotFound):
            self.app.disputes.advertiser_open_dispute(
                advertiser_telegram_user_id=99999,
                order_id=order["id"],
                reason="他人想偷开案",
            )

    def test_advertiser_dispute_requires_at_least_one_sent_delivery(self) -> None:
        _, order = self.create_approved_order()
        # No dispatch → no sent delivery
        with self.assertRaises(InvalidState):
            self.app.disputes.advertiser_open_dispute(
                advertiser_telegram_user_id=10001,
                order_id=order["id"],
                reason="还没发就想申诉",
            )

    def test_advertiser_can_stop_running_order_and_get_budget_back(self) -> None:
        _, order = self.create_approved_order(budget="10")
        self.confirm_timezone(10001, display_name="广告主")

        # Detail view should expose the stop button while order is approvable
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_stop_detail_1",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}",
                }
            }
        )
        keyboard_before = self._last_user_facing_keyboard()
        stop_buttons = [
            b for row in keyboard_before for b in row if b["callback_data"].endswith(":stop")
        ]
        self.assertEqual(len(stop_buttons), 1)

        # Tap stop → second confirmation page mentions refund amount
        confirm = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_stop_confirm",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}:stop",
                }
            }
        )
        self.assertEqual(confirm["type"], "callback_advertiser_order_stop_confirm")
        self.assertIn("USD 10.00", self._last_user_facing_text())

        # Confirm stop → status becomes paused, budget refunded, paused notification fires
        self.gateway.private_messages.clear()
        result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_stop_yes",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}:stop_yes",
                }
            }
        )
        self.assertEqual(result["type"], "callback_advertiser_order_stopped")

        with self.app.db.transaction() as conn:
            saved = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (order["id"],)).fetchone()
            advertiser = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(saved["reserved_cents"], 0)
        self.assertEqual(advertiser["available_balance_cents"], 1000)

        # Pause notification should have been sent
        pause_notifs = [m for m in self.gateway.private_messages if "投放已暂停" in m["text"]]
        self.assertEqual(len(pause_notifs), 1)
        self.assertIn("广告主主动停止投放", pause_notifs[0]["text"])

        # Detail page no longer shows stop button (terminal state)
        keyboard_after = self._last_user_facing_keyboard()
        stop_buttons_after = [
            b for row in keyboard_after for b in row if b["callback_data"].endswith(":stop")
        ]
        self.assertEqual(stop_buttons_after, [])

        # Audit row recorded
        with self.app.db.transaction() as conn:
            audit = conn.execute(
                "SELECT * FROM audit_logs WHERE entity_id = ? AND action = 'order_paused_by_advertiser'",
                (order["id"],),
            ).fetchone()
        self.assertIsNotNone(audit)

    def test_advertiser_stop_rejects_other_users_orders(self) -> None:
        _, order = self.create_approved_order()
        self.confirm_timezone(99999, display_name="他人")

        result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_stop_steal",
                    "from": {"id": 99999, "first_name": "他人"},
                    "message": {"chat": {"id": 99999}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}:stop_yes",
                }
            }
        )
        self.assertEqual(result["type"], "callback_advertiser_order_stop_not_found")

        # Original order should be untouched
        with self.app.db.transaction() as conn:
            saved = conn.execute("SELECT status FROM ad_orders WHERE id = ?", (order["id"],)).fetchone()
        self.assertEqual(saved["status"], "approved")

    def test_advertiser_stop_blocks_already_paused_order(self) -> None:
        _, order = self.create_approved_order()
        self.confirm_timezone(10001, display_name="广告主")
        # First stop succeeds
        self.app.orders.advertiser_pause_order(
            order_id=order["id"],
            advertiser_telegram_user_id=10001,
        )

        # Trying again must be rejected with a friendly message
        result = self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_stop_again",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}, "message_id": 1},
                    "data": f"advertiser:order:{order['id']}:stop_yes",
                }
            }
        )
        self.assertEqual(result["type"], "callback_advertiser_order_stop_invalid")
        self.assertIn("无法停止", self._last_user_facing_text())

    def test_placement_slot_switch_preserves_per_slot_creative_draft(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        std = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="标准插播草稿",
            target_url="https://example.com/std",
        )

        for cb_id, data in [
            ("cb_swap_1", f"channel:order:{channel['id']}"),
            ("cb_swap_2", "place:slot:standard_card"),
            ("cb_swap_3", "place:creative"),
            ("cb_swap_4", "place:pick:0"),
        ]:
            self.app.update_handler.handle(
                {
                    "callback_query": {
                        "id": cb_id,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "message": {"chat": {"id": 10001}},
                        "data": data,
                    }
                }
            )

        with self.app.db.transaction() as conn:
            payload_after_pick = json.loads(
                conn.execute(
                    "SELECT payload_json FROM bot_conversation_states WHERE chat_id = '10001'"
                ).fetchone()["payload_json"]
            )
        self.assertEqual(payload_after_pick["material_id"], std["id"])

        # Switch to 文字插播 — selection must clear so the user can configure the new slot fresh
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_swap_to_light",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:slot:light_tail",
                }
            }
        )
        with self.app.db.transaction() as conn:
            payload_on_light = json.loads(
                conn.execute(
                    "SELECT payload_json FROM bot_conversation_states WHERE chat_id = '10001'"
                ).fetchone()["payload_json"]
            )
        self.assertEqual(payload_on_light["slot_type"], "light_tail")
        self.assertNotIn("material_id", payload_on_light)
        self.assertNotIn("creative_text", payload_on_light)

        # Flip back to 标准插播 — the previous draft should be restored
        self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": "cb_swap_back",
                    "from": {"id": 10001, "first_name": "广告主"},
                    "message": {"chat": {"id": 10001}},
                    "data": "place:slot:standard_card",
                }
            }
        )
        with self.app.db.transaction() as conn:
            payload_restored = json.loads(
                conn.execute(
                    "SELECT payload_json FROM bot_conversation_states WHERE chat_id = '10001'"
                ).fetchone()["payload_json"]
            )
        self.assertEqual(payload_restored["slot_type"], "standard_card")
        self.assertEqual(payload_restored["material_id"], std["id"])
        self.assertEqual(payload_restored["creative_text"], "标准插播草稿")
        self.assertEqual(payload_restored["target_url"], "https://example.com/std")

    def _grant_publisher_access(self, channel: dict, telegram_user_id: int = 20001) -> None:
        """Wire FakeGateway so the publisher passes the get_chat_member check."""
        self.gateway.chat_members[(str(channel["telegram_chat_id"]), str(telegram_user_id))] = {
            "status": "creator",
            "can_post_messages": True,
            "can_edit_messages": True,
            "can_pin_messages": True,
        }

    def _publisher_callback(self, data: str, *, telegram_user_id: int = 20001, cb_id: str = "cb_pub") -> dict:
        return self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": cb_id,
                    "from": {"id": telegram_user_id, "first_name": "频道主"},
                    "message": {"chat": {"id": telegram_user_id}},
                    "data": data,
                }
            }
        )

    # ---------- AI-callable service-layer surface ----------

    def test_review_notes_land_on_audit_log_payload(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="审核备注测试文案",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"], note="素材已人工核对，符合插播规范")
        with self.app.db.transaction() as conn:
            audit = conn.execute(
                "SELECT payload_json FROM audit_logs WHERE entity_type = 'ad_order' "
                "AND entity_id = ? AND action = 'order_approved'",
                (order["id"],),
            ).fetchone()
        self.assertIsNotNone(audit)
        self.assertIn("素材已人工核对", audit["payload_json"])

    def test_review_notes_land_on_refund_audit_log(self) -> None:
        channel, order = self.create_approved_order()
        self.app.fulfillment.dispatch_due()
        with self.app.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
        self.app.orders.refund_delivery_partial(
            delivery["id"], money_to_cents("3.00"), "频道主提前删除", note="3 美金部分补偿，频道主同意"
        )
        with self.app.db.transaction() as conn:
            audit = conn.execute(
                "SELECT payload_json FROM audit_logs WHERE entity_type = 'delivery' "
                "AND entity_id = ? AND action = 'delivery_partially_refunded'",
                (delivery["id"],),
            ).fetchone()
        self.assertIn("3 美金部分补偿", audit["payload_json"])

    def test_admin_detail_timeline_merges_audit_and_evidence(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="时间线测试文案",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        url = self.start_http_server()
        # Approve with a note via HTTP form body, exercising the same path
        # the rendered <form> takes
        body = "note=人工核对通过".encode("utf-8")
        request = urllib.request.Request(
            f"{url}/admin/orders/{order['id']}/approve?token=admin-token",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request) as response:
            response.read()
        detail_request = urllib.request.Request(
            f"{url}/admin/orders/{order['id']}?token=admin-token",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(detail_request) as response:
            detail = json.loads(response.read())
        timeline = detail["order"]["时间线"]
        self.assertTrue(any(event["title"] == "order_approved" for event in timeline))
        approve_events = [event for event in timeline if event["title"] == "order_approved"]
        self.assertIn("人工核对通过", approve_events[0]["note"])
        # Evidence and audit kinds both appear on the same timeline
        kinds = {event["kind"] for event in timeline}
        self.assertEqual(kinds, {"操作", "证据"})

    def test_phase_one_golden_path_end_to_end(self) -> None:
        """Walks the施工图 §3 一期金线 in one pass:

        bind channel → fund advertiser via two-person review →
        placement configurator → operator approve with note →
        dispatch (3-button keyboard) → 查看详情 deep link → partial
        refund → self-promo publish → earnings settled.

        Auto-approve is turned off so the operator-approve path runs
        end-to-end (matches the production setting per施工图 §3).
        """
        # Rebuild the app with auto-approve off — exercises the production
        # path where operators must approve each order
        prod_settings = Settings(
            db_path=self.settings.db_path,
            bot_username="ChaBoTestBot",
            bot_auto_approve_orders=False,
        )
        self.app = create_app(prod_settings, self.gateway)

        # Two operator accounts for the topup approval double-check
        self.app.ledger.manual_topup(33333, money_to_cents("0.01"), display_name="申请人")
        self.app.ledger.manual_topup(44444, money_to_cents("0.01"), display_name="审批人")

        # 1. Bind channel and confirm timezones
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)

        # 2. Fund advertiser via the production-recommended two-person review
        request = self.app.topup_approvals.request_topup(
            recipient_telegram_user_id=10001,
            amount_cents=money_to_cents("20"),
            reason="OTC 收到 20 USDT 金线测试",
            requester_telegram_user_id=33333,
            evidence_url="https://evidence.example/preflight.png",
        )
        approved = self.app.topup_approvals.approve_topup(
            request_id=request["id"],
            approver_telegram_user_id=44444,
            approval_note="对账已核",
        )
        self.assertEqual(approved["status"], "approved")

        # 3. Placement configurator: open from channel deep link, pick slot,
        #    write material via the input flow, submit.
        for cb_id, data in [
            ("gp_1", f"channel:order:{channel['id']}"),
            ("gp_2", "place:slot:standard_card"),
            ("gp_3", "place:creative"),
            ("gp_4", "place:new:standard_card"),
        ]:
            self._advertiser_callback(data, cb_id=cb_id)
        for text in ["金线测试标准插播文案", "https://advertiser.example/landing"]:
            self.app.update_handler.handle(
                {
                    "message": {
                        "message_id": 9001,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "chat": {"id": 10001},
                        "text": text,
                    }
                }
            )
        submit = self._advertiser_callback("place:submit", cb_id="gp_submit")
        self.assertEqual(submit["type"], "callback_placement_order_created")
        order_id = submit["order_id"]

        # 4. Operator approves with a note (review-note path)
        self.app.orders.approve_order_for_operator(
            order_id=order_id,
            operator_telegram_user_id=33333,
            note="人工核对通过",
        )

        # 5. Dispatch the delivery; verify the 3-button keyboard
        dispatched = self.app.fulfillment.dispatch_due()
        self.assertEqual(dispatched[0]["status"], "sent")
        keyboard = self.gateway.sent_ads[-1]["inline_keyboard"]
        self.assertEqual([b["text"] for b in keyboard[0]], ["📣 频道招商", "🔍 查看详情"])
        self.assertIn(f"ch_{channel['ref_token']}", keyboard[0][0]["url"])
        self.assertIn("ad_del_", keyboard[0][1]["url"])

        # 6. A viewer follows the 查看详情 deep link
        with self.app.db.transaction() as conn:
            delivery = conn.execute(
                "SELECT * FROM deliveries WHERE order_id = ?", (order_id,)
            ).fetchone()
        self.confirm_timezone(555, display_name="点击用户")
        view = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 9100,
                    "from": {"id": 555, "first_name": "点击用户"},
                    "chat": {"id": 555},
                    "text": f"/start ad_{delivery['id']}",
                }
            }
        )
        self.assertEqual(view["type"], "ad_start")
        detail = self.gateway.private_messages[-1]
        self.assertIn("插播广告详情", detail["text"])
        # CTA + sales callback present
        flat = [b for row in detail["inline_keyboard"] for b in row]
        self.assertTrue(any(b.get("callback_data") == f"channel:order:{channel['id']}" for b in flat))

        # 7. Operator refunds part of the spend with a note
        self.app.orders.refund_delivery_for_operator(
            delivery_id=delivery["id"],
            reason="频道主提前删除部分时段",
            operator_telegram_user_id=33333,
            amount_cents=money_to_cents("3"),
            note="3 美元部分补偿",
        )

        # 8. Self-promo publish reusing the same library material
        with self.app.db.transaction() as conn:
            material_row = conn.execute(
                "SELECT id FROM creatives WHERE advertiser_account_id = "
                "(SELECT id FROM accounts WHERE telegram_user_id = '10001') LIMIT 1"
            ).fetchone()
        material_id = material_row["id"]
        # Publisher is also their own advertiser identity → create a self-promo
        # material via the publisher identity for self-promo
        pub_material = self.app.materials.create_material(
            advertiser_telegram_user_id=20001,
            format_type="standard_card",
            text="频道主自用文案",
            target_url="https://owner.example/post",
            button_text="查看详情",
            display_name="频道主",
        )
        self_promo_result = self._publisher_callback(
            f"pub:self:pick:{channel['ref_token']}:{pub_material['id']}",
            cb_id="gp_self",
        )
        self.assertEqual(self_promo_result["type"], "callback_self_promo_published")

        # 9. Confirm publisher earnings (post-observation) — at this point
        # the delivery is "sent"; advance time would normally happen via
        # confirm_due_earnings(observation_hours=0)
        self.app.fulfillment.confirm_due_earnings(observation_hours=0)
        with self.app.db.transaction() as conn:
            publisher = conn.execute(
                "SELECT * FROM accounts WHERE telegram_user_id = '20001'"
            ).fetchone()
        self.assertGreaterEqual(publisher["confirmed_earnings_cents"], 0)

        # 10. Detail-page timeline merges audit + evidence and includes the note
        url = self.start_http_server()
        detail_request = urllib.request.Request(
            f"{url}/admin/orders/{order_id}?token=admin-token",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(detail_request) as response:
            payload = json.loads(response.read())
        timeline = payload["order"]["时间线"]
        self.assertTrue(any(event["title"] == "order_approved" for event in timeline))
        approve_events = [e for e in timeline if e["title"] == "order_approved"]
        self.assertIn("人工核对通过", approve_events[0]["note"])

        # 11. Tool call audit captured the AI-callable boundary actions
        approve_logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=33333, tool_name="approve_order_for_operator"
        )
        self.assertEqual(approve_logs[0]["result_status"], "success")
        topup_logs = self.app.tool_call_logs.list_calls(tool_name="topup_approve")
        self.assertEqual(topup_logs[0]["result_status"], "success")

    def test_topup_approval_two_person_flow_moves_money_only_after_approve(self) -> None:
        # Make sure two distinct operator accounts exist
        self.app.ledger.manual_topup(33333, money_to_cents("0.01"), display_name="申请人")
        self.app.ledger.manual_topup(44444, money_to_cents("0.01"), display_name="审批人")
        request = self.app.topup_approvals.request_topup(
            recipient_telegram_user_id=10001,
            amount_cents=money_to_cents("12.34"),
            reason="OTC 收到 USDT 12.34，转入插播余额",
            requester_telegram_user_id=33333,
            evidence_url="https://evidence.example/screenshot.png",
        )
        # Pending request must NOT have moved money yet
        with self.app.db.transaction() as conn:
            row = conn.execute(
                "SELECT available_balance_cents FROM accounts WHERE telegram_user_id = '10001'"
            ).fetchone()
        self.assertIsNone(row)  # recipient account does not exist yet

        # Same-actor approval must be rejected — two-person rule
        with self.assertRaises(InvalidState):
            self.app.topup_approvals.approve_topup(
                request_id=request["id"],
                approver_telegram_user_id=33333,
            )
        # Approved by a different operator → ledger moves
        approved = self.app.topup_approvals.approve_topup(
            request_id=request["id"],
            approver_telegram_user_id=44444,
            approval_note="对账已核",
        )
        self.assertEqual(approved["status"], "approved")
        with self.app.db.transaction() as conn:
            recipient = conn.execute(
                "SELECT available_balance_cents FROM accounts WHERE telegram_user_id = '10001'"
            ).fetchone()
        self.assertEqual(recipient["available_balance_cents"], 1234)

        # Re-approving the same request must fail
        with self.assertRaises(InvalidState):
            self.app.topup_approvals.approve_topup(
                request_id=request["id"],
                approver_telegram_user_id=44444,
            )

    def test_topup_approval_reject_path_blocks_money_and_logs(self) -> None:
        self.app.ledger.manual_topup(33333, money_to_cents("0.01"), display_name="申请人")
        self.app.ledger.manual_topup(44444, money_to_cents("0.01"), display_name="审批人")
        request = self.app.topup_approvals.request_topup(
            recipient_telegram_user_id=20001,
            amount_cents=money_to_cents("99.00"),
            reason="疑似重复入账",
            requester_telegram_user_id=33333,
        )
        rejected = self.app.topup_approvals.reject_topup(
            request_id=request["id"],
            approver_telegram_user_id=44444,
            approval_note="申请人金额对不上凭证",
        )
        self.assertEqual(rejected["status"], "rejected")
        # Recipient (20001) account was never created — no money moved
        with self.app.db.transaction() as conn:
            recipient = conn.execute(
                "SELECT available_balance_cents FROM accounts WHERE telegram_user_id = '20001'"
            ).fetchone()
        self.assertIsNone(recipient)

        # Reject log lands in tool_call_logs as success (the action succeeded)
        logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=44444, tool_name="topup_reject"
        )
        self.assertEqual(logs[0]["result_status"], "success")

    def test_topup_request_validates_amount_and_reason(self) -> None:
        self.app.ledger.manual_topup(33333, money_to_cents("0.01"), display_name="申请人")
        with self.assertRaises(InvalidState):
            self.app.topup_approvals.request_topup(
                recipient_telegram_user_id=10001,
                amount_cents=0,
                reason="x",
                requester_telegram_user_id=33333,
            )
        with self.assertRaises(InvalidState):
            self.app.topup_approvals.request_topup(
                recipient_telegram_user_id=10001,
                amount_cents=money_to_cents("1.00"),
                reason="   ",
                requester_telegram_user_id=33333,
            )
        with self.assertRaises(NotFound):
            # Requester account does not exist
            self.app.topup_approvals.request_topup(
                recipient_telegram_user_id=10001,
                amount_cents=money_to_cents("1.00"),
                reason="ok",
                requester_telegram_user_id=99999,
            )

    def test_database_backup_writes_a_consistent_snapshot(self) -> None:
        # Seed some state, then back up
        self.bind_channel()
        self.app.ledger.manual_topup(10001, money_to_cents("5.00"), display_name="广告主")
        target = Path(self.tmp.name) / "backups" / "snapshot.sqlite3"
        written = self.app.db.backup_to(str(target))
        self.assertTrue(Path(written).exists())
        # The backup must contain the same data
        import sqlite3 as _sqlite3
        with _sqlite3.connect(str(target)) as conn:
            row = conn.execute(
                "SELECT available_balance_cents FROM accounts WHERE telegram_user_id = '10001'"
            ).fetchone()
        self.assertEqual(row[0], 500)

    def test_health_endpoint_includes_db_status_and_ops_counters(self) -> None:
        url = self.start_http_server()
        with urllib.request.urlopen(f"{url}/health") as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["db"], "ok")
        self.assertIn("ops", payload)
        for key in (
            "pending_review_orders",
            "running_orders",
            "sent_today",
            "open_disputes",
            "failed_recent",
            "scheduled_due",
        ):
            self.assertIn(key, payload["ops"])

    def test_admin_landing_renders_summary_cards(self) -> None:
        # Create an order in pending_review so the alert variant lights up
        channel = self.bind_channel()
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="待审核测试",
            target_url="https://example.com",
        )
        self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        url = self.start_http_server()
        request = urllib.request.Request(f"{url}/admin?token=admin-token")
        with urllib.request.urlopen(request) as response:
            html = response.read().decode("utf-8")
        self.assertIn("待审核订单", html)
        self.assertIn("Open 争议", html)
        # Pending order should land in the alert variant
        self.assertIn("summary-card alert", html)

    def test_token_strength_check_flags_weak_and_loopback_safe(self) -> None:
        from chabo.web import check_token_strength
        # On a public host, missing tokens should warn
        warns = check_token_strength(admin_token=None, webhook_secret=None, host="0.0.0.0")
        self.assertEqual(len(warns), 2)
        # Loopback gives no warnings on missing config
        warns = check_token_strength(admin_token=None, webhook_secret=None, host="127.0.0.1")
        self.assertEqual(warns, [])
        # Weak (short) token warns regardless of host
        warns = check_token_strength(admin_token="short", webhook_secret="changeme-pls", host="127.0.0.1")
        self.assertEqual(len(warns), 2)
        # Strong tokens give no warnings
        strong_admin = "x9k2L7m4Pq8rT5wYzNbFjC3Hd"
        strong_secret = "4Yh8m2KpW3qX9zV6cR1nB7tJfL5g"
        warns = check_token_strength(admin_token=strong_admin, webhook_secret=strong_secret, host="0.0.0.0")
        self.assertEqual(warns, [])

    def test_create_order_logs_via_tool_call_audit(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="工具调用埋点订单",
            target_url="https://example.com",
        )
        # AI session creates an order
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
            actor_kind="ai",
            session_id="sess_order_x1",
        )
        logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=10001, tool_name="create_order"
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["session_id"], "sess_order_x1")
        self.assertEqual(logs[0]["result_status"], "success")
        self.assertIn(order["id"], logs[0]["result_summary"])

        # Failure path: missing budget triggers InsufficientBalance via reserve
        # → logged as error
        from chabo.services import InsufficientBalance, ChaboError  # local import for the test
        with self.assertRaises(ChaboError):
            self.app.orders.create_order(
                advertiser_telegram_user_id=10001,
                channel_token=channel["ref_token"],
                slot_type="standard_card",
                material_id=material["id"],
                budget_cents=money_to_cents("10000"),  # blow past balance
                actor_kind="ai",
                session_id="sess_order_x2",
            )
        all_logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=10001, tool_name="create_order"
        )
        error_log = next(row for row in all_logs if row["result_status"] == "error")
        self.assertEqual(error_log["session_id"], "sess_order_x2")
        self.assertIn(error_log["error_type"], {"InsufficientBalance", "InvalidState"})

    def test_self_promo_prepare_publish_logs_via_tool_call_audit(self) -> None:
        channel = self.bind_channel()
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=20001,
            format_type="standard_card",
            text="自用埋点测试",
            target_url="https://example.com",
        )
        prepared = self.app.self_promos.prepare_publish(
            publisher_telegram_user_id=20001,
            channel_id=channel["id"],
            material_id=material["id"],
            actor_kind="ai",
            session_id="sess_sp_x1",
        )
        logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=20001, tool_name="self_promo_prepare_publish"
        )
        self.assertEqual(logs[0]["session_id"], "sess_sp_x1")
        self.assertIn(prepared["self_promo_id"], logs[0]["result_summary"])

        # Foreign user → NotFound logged as error
        with self.assertRaises(NotFound):
            self.app.self_promos.prepare_publish(
                publisher_telegram_user_id=99999,
                channel_id=channel["id"],
                material_id=material["id"],
                actor_kind="ai",
                session_id="sess_sp_x2",
            )
        all_logs = self.app.tool_call_logs.list_calls(
            tool_name="self_promo_prepare_publish"
        )
        statuses = {row["result_status"] for row in all_logs}
        self.assertIn("error", statuses)

    def test_manual_topup_logs_via_tool_call_audit(self) -> None:
        # Default actor_kind is admin since manual_topup is operator-driven
        result = self.app.ledger.manual_topup(
            10001,
            money_to_cents("12.34"),
            display_name="广告主",
            actor_telegram_user_id=33333,
            session_id="sess_topup_x1",
        )
        self.assertEqual(result["available_balance_cents"], 1234)
        logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=33333, tool_name="manual_topup"
        )
        self.assertEqual(logs[0]["actor_kind"], "admin")
        self.assertEqual(logs[0]["session_id"], "sess_topup_x1")
        self.assertIn("1234", logs[0]["result_summary"])
        # If the caller does not provide actor_telegram_user_id, the recipient's id is used
        self.app.ledger.manual_topup(20001, money_to_cents("1.00"))
        fallback = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=20001, tool_name="manual_topup"
        )
        self.assertEqual(fallback[0]["actor_kind"], "admin")

    def test_instrumented_services_log_success_and_failure(self) -> None:
        # Success path: AI session calls create_material
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="AI 自动创建的素材",
            target_url="https://example.com",
            actor_kind="ai",
            session_id="sess_ai_x1",
        )
        success_logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=10001, tool_name="create_material"
        )
        self.assertEqual(len(success_logs), 1)
        self.assertEqual(success_logs[0]["actor_kind"], "ai")
        self.assertEqual(success_logs[0]["session_id"], "sess_ai_x1")
        self.assertEqual(success_logs[0]["result_status"], "success")
        self.assertIn(material["id"], success_logs[0]["result_summary"])

        # Failure path: bad format_type triggers InvalidState and produces an error log
        with self.assertRaises(InvalidState):
            self.app.materials.create_material(
                advertiser_telegram_user_id=10001,
                format_type="pin24h",
                text="bad",
                target_url="https://example.com",
                actor_kind="ai",
                session_id="sess_ai_x1",
            )
        all_logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=10001, tool_name="create_material"
        )
        self.assertEqual(len(all_logs), 2)
        error_log = next(row for row in all_logs if row["result_status"] == "error")
        self.assertEqual(error_log["error_type"], "InvalidState")
        self.assertEqual(error_log["session_id"], "sess_ai_x1")

        # Operator wrapper logs as actor_kind=admin by default
        channel = self.bind_channel()
        self.topup_advertiser("10")
        self.app.ledger.manual_topup(33333, money_to_cents("0.01"), display_name="运营员")
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order_for_operator(
            order_id=order["id"],
            operator_telegram_user_id=33333,
        )
        op_logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=33333, tool_name="approve_order_for_operator"
        )
        self.assertEqual(op_logs[0]["actor_kind"], "admin")
        self.assertEqual(op_logs[0]["result_status"], "success")
        self.assertIn(order["id"], op_logs[0]["result_summary"])

        # Publisher wrapper logs the band switch
        self.app.channels.set_format_policy_for_publisher(
            publisher_telegram_user_id=20001,
            channel_id=channel["id"],
            format_type="standard_card",
            enabled=True,
            owner_price_band="high",
            actor_kind="ai",
            session_id="sess_pub_y1",
        )
        pub_logs = self.app.tool_call_logs.list_calls(
            actor_telegram_user_id=20001, tool_name="set_format_policy_for_publisher"
        )
        self.assertEqual(pub_logs[0]["session_id"], "sess_pub_y1")
        self.assertIn("high", pub_logs[0]["result_summary"])

    def test_tool_call_log_service_records_and_filters(self) -> None:
        # No actor → still recorded with NULL actor_account_id
        log_a = self.app.tool_call_logs.log_call(
            tool_name="create_material",
            actor_telegram_user_id=10001,
            actor_kind="ai",
            session_id="sess_1",
            arguments={"format_type": "standard_card", "text_preview": "..."},
            result_status="success",
            result_summary="created cre_xxx",
        )
        log_b = self.app.tool_call_logs.log_call(
            tool_name="approve_order",
            actor_telegram_user_id=99999,
            actor_kind="admin",
            arguments={"order_id": "ord_xxx"},
            result_status="error",
            error_type="InvalidState",
            result_summary="order already approved",
        )
        self.assertIsNone(log_a["actor_account_id"])  # 10001 has no account yet
        self.assertEqual(log_a["actor_kind"], "ai")
        self.assertEqual(log_b["result_status"], "error")
        self.assertEqual(log_b["error_type"], "InvalidState")

        # Filter by actor
        rows = self.app.tool_call_logs.list_calls(actor_telegram_user_id=10001)
        self.assertEqual({row["id"] for row in rows}, {log_a["id"]})

        # Filter by tool_name
        rows = self.app.tool_call_logs.list_calls(tool_name="approve_order")
        self.assertEqual({row["id"] for row in rows}, {log_b["id"]})

        # Filter by result_status
        rows = self.app.tool_call_logs.list_calls(result_status="error")
        self.assertEqual({row["id"] for row in rows}, {log_b["id"]})

        # Validation
        with self.assertRaises(InvalidState):
            self.app.tool_call_logs.log_call(tool_name="bad", actor_kind="robot")
        with self.assertRaises(InvalidState):
            self.app.tool_call_logs.log_call(tool_name="bad", result_status="maybe")

    def test_order_service_operator_wrappers_route_through_actor_account(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("10")
        # Make sure the operator has a real account (so the wrapper can resolve it)
        self.app.ledger.manual_topup(33333, money_to_cents("0.01"), display_name="运营员")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="审核测试文案",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )

        approved = self.app.orders.approve_order_for_operator(
            order_id=order["id"],
            operator_telegram_user_id=33333,
        )
        self.assertEqual(approved["status"], "approved")

        # audit_logs should record actor_account_id pointing at the operator's account
        with self.app.db.transaction() as conn:
            operator = conn.execute(
                "SELECT id FROM accounts WHERE telegram_user_id = '33333'"
            ).fetchone()
            audit = conn.execute(
                "SELECT * FROM audit_logs WHERE entity_type = 'ad_order' AND entity_id = ? "
                "AND action = 'order_approved'",
                (order["id"],),
            ).fetchone()
        self.assertEqual(audit["actor_account_id"], operator["id"])

        # Unknown operator must NotFound rather than fall through to None actor
        with self.assertRaises(NotFound):
            self.app.orders.reject_order_for_operator(
                order_id=order["id"],
                reason="x",
                operator_telegram_user_id=8888888,
            )

    def test_channel_service_publisher_write_apis_enforce_ownership(self) -> None:
        channel = self.bind_channel()
        # Owner (20001) can change band
        result = self.app.channels.set_format_policy_for_publisher(
            publisher_telegram_user_id=20001,
            channel_id=channel["id"],
            format_type="standard_card",
            enabled=True,
            owner_price_band="high",
        )
        self.assertEqual(result["owner_price_band"], "high")

        # Owner (20001) can change daily limit
        cfg = self.app.channels.set_daily_ad_limit_for_publisher(
            publisher_telegram_user_id=20001,
            channel_id=channel["id"],
            daily_ad_limit=5,
        )
        self.assertEqual(cfg["daily_ad_limit"], 5)

        # Foreign user cannot — must raise NotFound (not InvalidState), per the
        # AI tool boundary contract: never confirm a resource exists to non-owners.
        with self.assertRaises(NotFound):
            self.app.channels.set_format_policy_for_publisher(
                publisher_telegram_user_id=99999,
                channel_id=channel["id"],
                format_type="standard_card",
                enabled=False,
                owner_price_band="low",
            )
        with self.assertRaises(NotFound):
            self.app.channels.set_daily_ad_limit_for_publisher(
                publisher_telegram_user_id=99999,
                channel_id=channel["id"],
                daily_ad_limit=1,
            )
        # Non-existent telegram_user_id (no account) — same NotFound
        with self.assertRaises(NotFound):
            self.app.channels.set_daily_ad_limit_for_publisher(
                publisher_telegram_user_id=12345678,
                channel_id=channel["id"],
                daily_ad_limit=1,
            )

        # Original limits left intact for the owner
        with self.app.db.transaction() as conn:
            cfg_row = conn.execute(
                "SELECT daily_ad_limit FROM channel_configs WHERE channel_id = ?",
                (channel["id"],),
            ).fetchone()
            policy_row = conn.execute(
                "SELECT owner_price_band FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = 'standard_card'",
                (channel["id"],),
            ).fetchone()
        self.assertEqual(cfg_row["daily_ad_limit"], 5)
        self.assertEqual(policy_row["owner_price_band"], "high")

    def test_order_service_list_and_view_enforce_ownership(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="AI 工具调用测试文案",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )

        listed = self.app.orders.list_orders(advertiser_telegram_user_id=10001)
        self.assertEqual([row["id"] for row in listed], [order["id"]])
        self.assertEqual(listed[0]["channel_title"], channel["title"])

        # Foreign user gets empty list
        self.assertEqual(self.app.orders.list_orders(advertiser_telegram_user_id=99999), [])

        view = self.app.orders.get_order_view(order["id"], advertiser_telegram_user_id=10001)
        self.assertEqual(view["id"], order["id"])
        self.assertEqual(view["creative"]["text"], "AI 工具调用测试文案")
        self.assertEqual(view["channel"]["ref_token"], channel["ref_token"])
        self.assertEqual(view["slot_type"], "standard_card")

        with self.assertRaises(NotFound):
            self.app.orders.get_order_view(order["id"], advertiser_telegram_user_id=99999)

    def test_channel_service_view_bundles_config_policies_rates_and_stats(self) -> None:
        channel = self.bind_channel()
        # ensure rates / policies are seeded by triggering a quote
        self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="news",
            median_24h_views=10_000,
            light_unique_clickers_30d=10,
            risk_level="normal",
        )

        listed = self.app.channels.list_publisher_channels(publisher_telegram_user_id=20001)
        self.assertEqual([row["id"] for row in listed], [channel["id"]])
        self.assertEqual(self.app.channels.list_publisher_channels(publisher_telegram_user_id=88888), [])

        view = self.app.channels.get_channel_view(
            channel["id"], publisher_telegram_user_id=20001
        )
        self.assertEqual(view["id"], channel["id"])
        self.assertIsNotNone(view["config"])
        format_types = {p["format_type"] for p in view["format_policies"]}
        self.assertIn("standard_card", format_types)
        self.assertTrue(view["rate_cards"])
        self.assertEqual(view["today_ads"], 0)

        with self.assertRaises(NotFound):
            self.app.channels.get_channel_view(channel["id"], publisher_telegram_user_id=88888)

    def test_ledger_service_summaries_match_account_state(self) -> None:
        # Wallet summary from advertiser-only top up
        self.app.ledger.manual_topup(10001, money_to_cents("12.50"), display_name="广告主")
        wallet = self.app.ledger.get_wallet_summary(telegram_user_id=10001)
        self.assertEqual(wallet["available_balance_cents"], 1250)
        self.assertEqual(wallet["reserved_balance_cents"], 0)

        # Unknown user returns zeros
        empty = self.app.ledger.get_wallet_summary(telegram_user_id=77777)
        self.assertEqual(empty["available_balance_cents"], 0)
        self.assertNotIn("account_id", empty)

        # Earnings summary from a delivered ad
        channel = self.bind_channel()
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="收益服务测试",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()

        earnings = self.app.ledger.get_earnings_summary(telegram_user_id=20001)
        self.assertGreater(earnings["pending_earnings_cents"], 0)

        breakdown = self.app.ledger.list_channel_earnings(publisher_telegram_user_id=20001)
        self.assertEqual([row["channel_id"] for row in breakdown], [channel["id"]])
        self.assertEqual(breakdown[0]["pending_cents"], earnings["pending_earnings_cents"])
        self.assertEqual(breakdown[0]["delivery_count"], 1)
        self.assertEqual(self.app.ledger.list_channel_earnings(publisher_telegram_user_id=88888), [])

    def _advertiser_callback(self, data: str, *, telegram_user_id: int = 10001, cb_id: str = "cb_adv") -> dict:
        return self.app.update_handler.handle(
            {
                "callback_query": {
                    "id": cb_id,
                    "from": {"id": telegram_user_id, "first_name": "广告主"},
                    "message": {"chat": {"id": telegram_user_id}},
                    "data": data,
                }
            }
        )

    def test_publisher_earnings_page_offers_channel_and_statement_entries(self) -> None:
        self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        result = self._publisher_callback("publisher:earnings", cb_id="cb_earn_open")
        self.assertEqual(result["type"], "callback_publisher_earnings")
        message = self.gateway.private_messages[-1]
        self.assertIn("我的收益", message["text"])
        callbacks = [b.get("callback_data") for row in message["inline_keyboard"] for b in row]
        self.assertIn("earnings:channels", callbacks)
        self.assertIn("earnings:statement", callbacks)

    def test_earnings_channels_lists_per_channel_breakdown(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)
        # Create one delivery so the channel has an entry
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="频道分布测试",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()

        result = self._publisher_callback("earnings:channels", cb_id="cb_earn_channels")
        self.assertEqual(result["type"], "callback_earnings_channels")
        message = self.gateway.private_messages[-1]
        self.assertIn("频道分布", message["text"])
        self.assertIn(channel["title"], message["text"])
        self.assertIn("待确认", message["text"])
        callbacks = [b.get("callback_data") for row in message["inline_keyboard"] for b in row]
        self.assertIn(f"pub:channel:{channel['ref_token']}", callbacks)

    def test_earnings_statement_filters_to_publisher_side_entries(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)
        # Mixed user — also takes a manual topup so the wallet-side ledger has entries
        self.app.ledger.manual_topup(20001, money_to_cents("10"), display_name="频道主")
        # Generate a delivery so publisher_pending_earning is recorded
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="收益流水测试",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()

        result = self._publisher_callback("earnings:statement", cb_id="cb_earn_stmt")
        self.assertEqual(result["type"], "callback_earnings_statement")
        text = self.gateway.private_messages[-1]["text"]
        self.assertIn("收益流水", text)
        self.assertIn("频道入账", text)
        # manual_topup belongs to wallet side and must NOT appear here
        self.assertNotIn("人工入账", text)

    def test_wallet_balance_page_offers_topup_reserved_statement_buttons(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("20")
        result = self._advertiser_callback("advertiser:balance", cb_id="cb_wallet_open")
        self.assertEqual(result["type"], "callback_advertiser_balance")
        callbacks = [
            b.get("callback_data")
            for row in self.gateway.private_messages[-1]["inline_keyboard"]
            for b in row
        ]
        self.assertIn("wallet:topup", callbacks)
        self.assertIn("wallet:reserved", callbacks)
        self.assertIn("wallet:statement", callbacks)

    def test_wallet_topup_pick_sends_stars_invoice(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        result = self._advertiser_callback("wallet:topup:500", cb_id="cb_wallet_topup_500")
        self.assertEqual(result["type"], "callback_wallet_topup_invoice_sent")
        self.assertEqual(result["stars_amount"], 500)
        invoice = self.gateway.invoices[-1]
        self.assertEqual(invoice["chat_id"], "10001")
        self.assertEqual(invoice["currency"], "XTR")
        self.assertEqual(invoice["prices"][0]["amount"], 500)
        with self.app.db.transaction() as conn:
            intent = conn.execute(
                "SELECT * FROM stars_payment_intents WHERE buyer_account_id = (SELECT id FROM accounts WHERE telegram_user_id = '10001')"
            ).fetchone()
        self.assertEqual(intent["stars_amount"], 500)
        self.assertEqual(intent["status"], "pending")

    def test_wallet_reserved_lists_active_reserved_orders(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        channel = self.bind_channel()
        self.topup_advertiser("20")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="冻结测试文案",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        result = self._advertiser_callback("wallet:reserved", cb_id="cb_wallet_reserved")
        self.assertEqual(result["type"], "callback_wallet_reserved")
        text = self.gateway.private_messages[-1]["text"]
        self.assertIn("冻结明细", text)
        self.assertIn(channel["title"], text)
        self.assertIn(order["id"], text)

    def test_wallet_statement_lists_recent_transactions(self) -> None:
        self.confirm_timezone(10001, display_name="广告主")
        self.topup_advertiser("15")
        result = self._advertiser_callback("wallet:statement", cb_id="cb_wallet_statement")
        self.assertEqual(result["type"], "callback_wallet_statement")
        text = self.gateway.private_messages[-1]["text"]
        self.assertIn("账单流水", text)
        self.assertIn("人工入账", text)
        self.assertIn("15.00", text)

    def test_placement_submit_insufficient_balance_offers_topup_button(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(10001, display_name="广告主")
        # No topup → insufficient balance
        for cb_id, data in [
            ("cb_ins_open", f"channel:order:{channel['id']}"),
            ("cb_ins_slot", "place:slot:standard_card"),
            ("cb_ins_creative", "place:creative"),
            ("cb_ins_new", "place:new:standard_card"),
        ]:
            self._advertiser_callback(data, cb_id=cb_id)
        # Send creative text and url to finish material creation
        for text in ["这是预算不足测试的文案", "https://example.com"]:
            self.app.update_handler.handle(
                {
                    "message": {
                        "message_id": 99,
                        "from": {"id": 10001, "first_name": "广告主"},
                        "chat": {"id": 10001},
                        "text": text,
                    }
                }
            )
        submit = self._advertiser_callback("place:submit", cb_id="cb_ins_submit")
        self.assertEqual(submit["type"], "callback_placement_submit_failed")
        callbacks = [
            b.get("callback_data")
            for row in self.gateway.private_messages[-1]["inline_keyboard"]
            for b in row
        ]
        self.assertIn("wallet:topup", callbacks)

    def test_publisher_self_promo_panel_lists_publishable_materials(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)
        # publisher (20001) creates a standard_card material via the merged advertiser identity
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=20001,
            format_type="standard_card",
            text="自用频道运营内容",
            target_url="https://owner.example/post",
            button_text="去看看",
            display_name="频道主",
        )
        # light_tail materials are filtered out
        self.app.materials.create_material(
            advertiser_telegram_user_id=20001,
            format_type="light_tail",
            text="文字插播详情",
            target_url="https://owner.example/light",
            light_short_text="去看看",
        )

        result = self._publisher_callback(f"pub:self:{channel['ref_token']}", cb_id="cb_self_open")
        self.assertEqual(result["type"], "callback_publisher_self_promo")
        message = self.gateway.private_messages[-1]
        self.assertIn("自用发布", message["text"])
        button_callbacks = [b.get("callback_data") for row in message["inline_keyboard"] for b in row]
        self.assertIn(f"pub:self:pick:{channel['ref_token']}:{material['id']}", button_callbacks)
        # light_tail material should not appear
        self.assertNotIn(
            any(material["id"] in (b.get("callback_data") or "") for row in message["inline_keyboard"] for b in row if "light" in (b.get("text") or "")),
            [True],
        )

    def test_publisher_self_promo_publishes_and_records_row(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=20001,
            format_type="standard_card",
            text="自用标准插播文案",
            target_url="https://owner.example/landing",
            button_text="立即购买",
        )

        result = self._publisher_callback(
            f"pub:self:pick:{channel['ref_token']}:{material['id']}",
            cb_id="cb_self_publish",
        )
        self.assertEqual(result["type"], "callback_self_promo_published")

        sent = self.gateway.sent_ads[-1]
        self.assertEqual(sent["chat_id"], str(channel["telegram_chat_id"]))
        self.assertEqual(sent["text"], "自用标准插播文案")
        keyboard = sent["inline_keyboard"]
        self.assertEqual([b["text"] for b in keyboard[0]], ["📣 频道招商", "🔍 查看详情"])
        self.assertIn(f"ch_{channel['ref_token']}", keyboard[0][0]["url"])
        self.assertIn(f"sp_{result['self_promo_id']}", keyboard[0][1]["url"])
        self.assertEqual(keyboard[1], [{"text": "立即购买", "url": "https://owner.example/landing"}])

        with self.app.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM self_promo_publishes WHERE id = ?",
                (result["self_promo_id"],),
            ).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["channel_id"], channel["id"])
        self.assertEqual(row["creative_id"], material["id"])
        self.assertEqual(row["message_id"], sent["message_id"])

    def test_self_promo_deep_link_renders_detail_page(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=20001,
            format_type="standard_card",
            text="自用文案",
            target_url="https://owner.example/landing",
            button_text="立即查看",
        )
        publish = self._publisher_callback(
            f"pub:self:pick:{channel['ref_token']}:{material['id']}",
            cb_id="cb_self_publish_for_detail",
        )

        self.confirm_timezone(444, display_name="点击用户")
        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 88,
                    "from": {"id": 444, "first_name": "点击用户"},
                    "chat": {"id": 444},
                    "text": f"/start sp_{publish['self_promo_id']}",
                }
            }
        )
        self.assertEqual(result["type"], "self_promo_start")
        detail = self.gateway.private_messages[-1]
        self.assertIn("插播广告详情", detail["text"])
        self.assertIn(channel["title"], detail["text"])
        flat = [b for row in detail["inline_keyboard"] for b in row]
        self.assertTrue(any("立即查看" == b.get("text") and b.get("url") == "https://owner.example/landing" for b in flat))
        self.assertTrue(any(b.get("callback_data") == f"channel:order:{channel['id']}" for b in flat))

    def test_publisher_self_promo_rejects_foreign_channel(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)
        # Material owned by a different user
        foreign_material = self.app.materials.create_material(
            advertiser_telegram_user_id=99999,
            format_type="standard_card",
            text="外部素材",
            target_url="https://other.example",
        )
        result = self._publisher_callback(
            f"pub:self:pick:{channel['ref_token']}:{foreign_material['id']}",
            cb_id="cb_self_foreign",
        )
        self.assertEqual(result["type"], "callback_self_promo_failed")
        self.assertEqual(self.gateway.sent_ads, [])

    def test_publisher_channel_dashboard_renders_status_card_and_grid(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)
        # Generate one delivery so today_ads = 1 and pending earnings > 0
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="测试投放",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()

        result = self._publisher_callback(f"pub:channel:{channel['ref_token']}")
        self.assertEqual(result["type"], "callback_publisher_channel")
        message = self.gateway.private_messages[-1]
        text = message["text"]
        self.assertIn("今日广告：1 / 3", text)
        self.assertIn("可投形态：", text)
        self.assertIn("当前档位：中档", text)
        self.assertIn("待确认收益：USD", text)

        button_texts = [b["text"] for row in message["inline_keyboard"] for b in row]
        for label in [
            "⚙️ 接广告设置",
            "💵 价格档位",
            "🧩 展示形态",
            "⏱ 频控时间",
            "🪧 自用发布",
            "📊 数据",
            "💸 收益明细",
        ]:
            self.assertIn(label, button_texts)

    def test_publisher_band_picker_switches_owner_price_band(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)

        # Open picker
        result = self._publisher_callback(f"pub:band:{channel['ref_token']}", cb_id="cb_band_open")
        self.assertEqual(result["type"], "callback_publisher_band_picker")
        self.assertIn("中档 1.0x", self.gateway.private_messages[-1]["text"])

        # Switch standard_card to high
        switch = self._publisher_callback(
            f"pub:band:set:{channel['ref_token']}:standard_card:high",
            cb_id="cb_band_high",
        )
        self.assertEqual(switch["type"], "callback_publisher_band_set")

        with self.app.db.transaction() as conn:
            policy = conn.execute(
                "SELECT owner_price_band FROM channel_ad_format_policies WHERE channel_id = ? AND format_type = 'standard_card'",
                (channel["id"],),
            ).fetchone()
        self.assertEqual(policy["owner_price_band"], "high")
        self.assertIn("高档 1.25x", self.gateway.private_messages[-1]["text"])

    def test_publisher_limit_panel_adjusts_daily_limit(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)

        result = self._publisher_callback(f"pub:limit:{channel['ref_token']}", cb_id="cb_limit_open")
        self.assertEqual(result["type"], "callback_publisher_limit_panel")
        self.assertIn("每日最多：3 条", self.gateway.private_messages[-1]["text"])

        bumped = self._publisher_callback(f"pub:limit:set:{channel['ref_token']}:5", cb_id="cb_limit_5")
        self.assertEqual(bumped["type"], "callback_publisher_limit_set")
        self.assertIn("每日最多：5 条", self.gateway.private_messages[-1]["text"])

        with self.app.db.transaction() as conn:
            cfg = conn.execute(
                "SELECT daily_ad_limit FROM channel_configs WHERE channel_id = ?",
                (channel["id"],),
            ).fetchone()
        self.assertEqual(cfg["daily_ad_limit"], 5)

    def test_publisher_channel_earnings_shows_channel_scoped_total(self) -> None:
        channel = self.bind_channel()
        self.confirm_timezone(20001, role="publisher", display_name="频道主")
        self._grant_publisher_access(channel)

        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="测试投放",
            target_url="https://example.com",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()

        result = self._publisher_callback(f"pub:earnings:{channel['ref_token']}", cb_id="cb_earn")
        self.assertEqual(result["type"], "callback_publisher_channel_earnings")
        text = self.gateway.private_messages[-1]["text"]
        self.assertIn("收益明细", text)
        self.assertIn(channel["title"], text)
        self.assertIn("待确认", text)
        self.assertIn("已确认", text)
        self.assertIn("平台已收", text)

    def test_view_detail_deep_link_renders_full_ad_page(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="标准插播完整文案，点击查看详情后展示。",
            target_url="https://advertiser.example/landing",
            button_text="立即购买",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()
        with self.app.db.transaction() as conn:
            delivery = conn.execute(
                "SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)
            ).fetchone()

        self.confirm_timezone(333, display_name="点击用户")
        result = self.app.update_handler.handle(
            {
                "message": {
                    "message_id": 77,
                    "from": {"id": 333, "first_name": "点击用户"},
                    "chat": {"id": 333},
                    "text": f"/start ad_{delivery['id']}",
                }
            }
        )
        self.assertEqual(result["type"], "ad_start")

        detail = self.gateway.private_messages[-1]
        self.assertIn("插播广告详情", detail["text"])
        self.assertIn("标准插播完整文案", detail["text"])
        self.assertIn(channel["title"], detail["text"])

        keyboard = detail["inline_keyboard"]
        flat = [b for row in keyboard for b in row]
        cta = next((b for b in flat if b.get("text") == "立即购买"), None)
        self.assertIsNotNone(cta)
        self.assertEqual(cta["url"], "https://advertiser.example/landing")

        sales = next((b for b in flat if "想在这个频道投广告" in b.get("text", "")), None)
        self.assertIsNotNone(sales)
        self.assertEqual(sales["callback_data"], f"channel:order:{channel['id']}")

        with self.app.db.transaction() as conn:
            metric = conn.execute(
                "SELECT * FROM metric_snapshots WHERE delivery_id = ? AND metric_type = 'bot_start'",
                (delivery["id"],),
            ).fetchone()
        self.assertIsNotNone(metric)

    def test_standard_placement_publishes_three_button_keyboard(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("10")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="standard_card",
            text="标准插播文案",
            target_url="https://advertiser.example/landing",
            button_text="立即购买",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            material_id=material["id"],
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        dispatched = self.app.fulfillment.dispatch_due()
        self.assertEqual(dispatched[0]["status"], "sent")

        keyboard = self.gateway.sent_ads[0]["inline_keyboard"]
        self.assertEqual(len(keyboard), 2)
        top_row = keyboard[0]
        self.assertEqual([b["text"] for b in top_row], ["📣 频道招商", "🔍 查看详情"])
        self.assertIn(f"ch_{channel['ref_token']}", top_row[0]["url"])
        self.assertIn("ad_del_", top_row[1]["url"])
        self.assertEqual(keyboard[1], [{"text": "立即购买", "url": "https://advertiser.example/landing"}])

    def test_strong_placement_also_publishes_three_buttons(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")
        material = self.app.materials.create_material(
            advertiser_telegram_user_id=10001,
            format_type="strong_post",
            text="定制插播文案",
            target_url="https://advertiser.example/strong",
            button_text="开始使用",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="strong_post",
            material_id=material["id"],
            budget_cents=money_to_cents("20"),
        )
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()

        keyboard = self.gateway.sent_ads[0]["inline_keyboard"]
        self.assertEqual([b["text"] for b in keyboard[0]], ["📣 频道招商", "🔍 查看详情"])
        self.assertEqual(keyboard[1][0]["text"], "开始使用")

    def test_offer_acceptance_records_library_material(self) -> None:
        channel = self.bind_channel()
        self.topup_advertiser("20")
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
            slot_type="standard_card",
            offered_price_cents=money_to_cents("3"),
            creative_text="砍价插播文案",
            target_url="https://example.com",
            budget_cents=money_to_cents("3"),
            message="3 美金试试",
        )
        result = self.app.price_offers.respond_offer(offer["id"], accepted=True)
        self.assertEqual(result["status"], "accepted")
        order_id = result["accepted_order_id"]
        with self.app.db.transaction() as conn:
            order_row = conn.execute(
                "SELECT creative_id FROM ad_orders WHERE id = ?", (order_id,)
            ).fetchone()
            creative_row = conn.execute(
                "SELECT advertiser_account_id, format_type FROM creatives WHERE id = ?",
                (order_row["creative_id"],),
            ).fetchone()
        self.assertIsNotNone(creative_row["advertiser_account_id"])
        self.assertEqual(creative_row["format_type"], "standard_card")


if __name__ == "__main__":
    unittest.main()
