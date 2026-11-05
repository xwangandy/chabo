from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixtures import FakeGateway
from chabo.app import create_app
from chabo.config import Settings
from chabo.telegram import TelegramError


class _PrivateMessageFailingGateway(FakeGateway):
    """Simulates an advertiser/publisher who never started or blocked the Bot."""

    def send_private_message(self, *, chat_id: str | int, text: str, **kwargs: object) -> str:
        raise TelegramError("Forbidden: bot was blocked by the user")


class TestFulfillmentResilienceAndSettlement(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "s04.sqlite3")
        self.gateway = FakeGateway()
        self.settings = Settings(
            db_path=self.db_path,
            bot_username="chabo_test_bot",
            admin_token="admin-token",
            bot_auto_approve_orders=False,
        )
        self.app = create_app(self.settings, self.gateway)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_private_message_failure_in_budget_notify_does_not_rollback_or_resend(self) -> None:
        failing_gw = _PrivateMessageFailingGateway()
        app = create_app(self.settings, failing_gw)
        app.ledger.manual_topup(10001, 2000, display_name="未启动Bot的广告主")
        channel = app.channels.bind_channel(
            telegram_chat_id=-1009991,
            title="防重发测试频道",
            username="no_resend_chan",
            owner_telegram_user_id=20001,
            owner_display_name="频道主",
        )
        order = app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="单次投放预算刚好耗尽触发20%告警",
            target_url="https://example.com/ad",
            budget_cents=1000,
        )
        app.orders.approve_order(order["id"])

        dispatched_1 = app.fulfillment.dispatch_due()
        self.assertEqual(len(dispatched_1), 1)
        self.assertEqual(dispatched_1[0]["status"], "sent")
        self.assertEqual(len(failing_gw.sent_ads), 1)

        # Second dispatch_due must NOT re-send the already delivered ad
        dispatched_2 = app.fulfillment.dispatch_due()
        self.assertEqual(dispatched_2, [])
        self.assertEqual(len(failing_gw.sent_ads), 1)

        with app.db.connect() as conn:
            adv = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '10001'").fetchone()
            self.assertEqual(int(adv["spent_balance_cents"]), 1000)
            self.assertEqual(int(adv["reserved_balance_cents"]), 0)

    def test_pin_failure_preserves_message_id_for_reconciliation(self) -> None:
        self.gateway.fail_pin = True
        self.app.ledger.manual_topup(10001, 5000, display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-1009992,
            title="置顶失败记录message_id频道",
            username="pin_fail_chan",
            owner_telegram_user_id=20001,
            owner_display_name="频道主",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="pin24h",
            text="置顶广告测试",
            target_url="https://example.com/pin",
            budget_cents=2500,
        )
        self.app.orders.approve_order(order["id"])

        res = self.app.fulfillment.dispatch_due()
        self.assertEqual(res[0]["status"], "failed")
        with self.app.db.connect() as conn:
            deliv = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
            self.assertEqual(deliv["status"], "failed")
            self.assertIsNotNone(deliv["message_id"])

    def test_channel_rebind_during_inflight_order_does_not_break_settlement_or_holdback(self) -> None:
        self.app.ledger.manual_topup(10001, 5000, display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-1009993,
            title="换绑保护测试频道",
            username="rebind_chan",
            owner_telegram_user_id=20001,
            owner_display_name="原频道主",
        )
        # Non-premium publisher disables promo tail (pays 5% service fee) and sets high price band (1250 cents)
        self.app.channels.set_format_policy(
            channel["id"],
            "standard_card",
            enabled=True,
            owner_price_band="high",
            platform_promo_enabled=False,
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            text="高档位+5%服务费测试",
            target_url="https://example.com/band",
            budget_cents=1250,
        )
        self.assertEqual(int(order["unit_price_cents"]), 1250)
        self.app.orders.approve_order(order["id"])
        self.app.fulfillment.dispatch_due()

        # Another admin tries to re-bind the same channel before 24h settlement
        self.app.channels.bind_channel(
            telegram_chat_id=-1009993,
            title="换绑保护测试频道(新名)",
            username="rebind_chan",
            owner_telegram_user_id=20099,
            owner_display_name="新管理员",
        )

        # 24h confirmation and 7d holdback release must succeed and credit the original publisher (20001)
        confirmed_count = self.app.fulfillment.confirm_due_earnings(observation_hours=0)
        self.assertEqual(confirmed_count, 1)
        released_count = self.app.fulfillment.release_due_holdbacks(holdback_days=0)
        self.assertEqual(released_count, 1)

        with self.app.db.connect() as conn:
            orig_pub = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '20001'").fetchone()
            platform = conn.execute("SELECT * FROM accounts WHERE id = 'platform'").fetchone()
            # 1250 cents gross -> 5% fee = 62 cents, publisher net = 1188 cents
            self.assertEqual(int(platform["available_balance_cents"]), 62)
            self.assertEqual(int(orig_pub["pending_earnings_cents"]), 0)
            self.assertEqual(int(orig_pub["confirmed_earnings_cents"]), 1188)
            self.assertEqual(int(orig_pub["releasable_earnings_cents"]), 1188)


if __name__ == "__main__":
    unittest.main()
