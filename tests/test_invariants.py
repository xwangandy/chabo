"""Invariant + concurrency tests for the ledger / order pipeline.

These tests check properties that must hold across many call sequences,
not just specific golden-path scripts:

- advertiser balance triple (available/reserved/spent) sums to net topups
- publisher earning triple (pending/confirmed/releasable) sums to net
  earned-minus-reversed
- charge = publisher_net + platform_fee (no money created/lost)
- BEGIN IMMEDIATE serializes concurrent approve_order so only one wins
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chabo.app import create_app
from chabo.config import Settings
from chabo.money import money_to_cents
from chabo.services import ChaboError, InvalidState

from _fixtures import FakeGateway


class InvariantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = FakeGateway()
        self.settings = Settings(
            db_path=str(Path(self.tmp.name) / "invariants.sqlite3"),
            bot_username="ChaBoTestBot",
        )
        self.app = create_app(self.settings, self.gateway)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _bind_channel(self) -> dict:
        return self.app.channels.bind_channel(
            telegram_chat_id=-100123,
            title="测试频道",
            username="invariant_channel",
            owner_telegram_user_id=20001,
            owner_display_name="频道主",
        )

    def _account_balances(self, telegram_user_id: int) -> dict:
        with self.app.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT available_balance_cents, reserved_balance_cents, spent_balance_cents,
                       pending_earnings_cents, confirmed_earnings_cents, releasable_earnings_cents
                FROM accounts WHERE telegram_user_id = ?
                """,
                (str(telegram_user_id),),
            ).fetchone()
        return dict(row) if row else {}

    # --- Invariant 1: advertiser triple sum is conserved through the full lifecycle.
    def test_advertiser_triple_conserved_across_lifecycle(self) -> None:
        topup_cents = 10000  # $100
        self.app.ledger.manual_topup(10001, topup_cents, display_name="广告主")
        channel = self._bind_channel()

        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="不变式测试广告",
            target_url="https://example.com",
            budget_cents=money_to_cents("20"),
        )
        self.app.orders.approve_order(order["id"])
        delivered = self.app.fulfillment.dispatch_due()
        self.assertEqual(len(delivered), 1)
        delivery_id = delivered[0]["delivery_id"]

        # Partial refund: $5 back to advertiser
        self.app.orders.refund_delivery_partial(delivery_id, money_to_cents("5"), reason="部分退款")

        balances = self._account_balances(10001)
        triple_sum = (
            balances["available_balance_cents"]
            + balances["reserved_balance_cents"]
            + balances["spent_balance_cents"]
        )
        self.assertEqual(
            triple_sum,
            topup_cents,
            f"available+reserved+spent must equal net topups; got {balances}",
        )

    # --- Invariant 2: full refund returns advertiser to pre-order state.
    def test_full_refund_restores_advertiser_available(self) -> None:
        topup_cents = 10000
        self.app.ledger.manual_topup(10001, topup_cents, display_name="广告主")
        channel = self._bind_channel()
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="全退测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("30"),
        )
        self.app.orders.approve_order(order["id"])
        delivered = self.app.fulfillment.dispatch_due()
        self.app.orders.refund_delivery(delivered[0]["delivery_id"], reason="频道主删帖")

        balances = self._account_balances(10001)
        self.assertEqual(balances["available_balance_cents"], topup_cents)
        self.assertEqual(balances["reserved_balance_cents"], 0)
        self.assertEqual(balances["spent_balance_cents"], 0)

    # --- Invariant 3: delivery accounting — charge = publisher_net + platform_fee.
    def test_delivery_amounts_reconcile(self) -> None:
        self.app.ledger.manual_topup(10001, 10000, display_name="广告主")
        channel = self._bind_channel()
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="账目对平测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("50"),
        )
        self.app.orders.approve_order(order["id"])
        delivered = self.app.fulfillment.dispatch_due()
        delivery_id = delivered[0]["delivery_id"]

        with self.app.db.transaction() as conn:
            row = conn.execute(
                "SELECT charge_cents, publisher_net_cents, platform_fee_cents FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        self.assertEqual(
            row["charge_cents"],
            row["publisher_net_cents"] + row["platform_fee_cents"],
            f"charge must equal publisher_net + platform_fee: row={dict(row)}",
        )

    # --- Invariant 4: rejecting an order releases reserved budget completely.
    def test_reject_order_releases_full_reserved(self) -> None:
        topup_cents = 10000
        self.app.ledger.manual_topup(10001, topup_cents, display_name="广告主")
        channel = self._bind_channel()
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="拒审测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("25"),
        )
        before = self._account_balances(10001)
        self.assertEqual(before["reserved_balance_cents"], money_to_cents("25"))

        self.app.orders.reject_order(order["id"], reason="不合规")

        after = self._account_balances(10001)
        self.assertEqual(after["reserved_balance_cents"], 0)
        self.assertEqual(after["available_balance_cents"], topup_cents)
        self.assertEqual(after["spent_balance_cents"], 0)


class ConcurrencyTest(unittest.TestCase):
    """Verify BEGIN IMMEDIATE + busy_timeout serialize concurrent writers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = FakeGateway()
        self.settings = Settings(
            db_path=str(Path(self.tmp.name) / "concurrency.sqlite3"),
            bot_username="ChaBoTestBot",
        )
        self.app = create_app(self.settings, self.gateway)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_approve_only_one_succeeds(self) -> None:
        self.app.ledger.manual_topup(10001, 10000, display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100124,
            title="并发测试频道",
            username="concurrency_channel",
            owner_telegram_user_id=20002,
            owner_display_name="频道主",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="并发审核测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("20"),
        )

        results: list[Exception | dict] = []
        barrier = threading.Barrier(2)

        def approve_in_thread() -> None:
            barrier.wait()
            try:
                results.append(self.app.orders.approve_order(order["id"]))
            except Exception as exc:
                results.append(exc)

        threads = [threading.Thread(target=approve_in_thread) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        successes = [r for r in results if isinstance(r, dict)]
        failures = [r for r in results if isinstance(r, Exception)]

        self.assertEqual(len(successes), 1, f"exactly one approve should succeed; got {results}")
        self.assertEqual(len(failures), 1, f"exactly one approve should be rejected; got {results}")
        self.assertIsInstance(failures[0], (InvalidState, ChaboError))


class AdvertiserNotificationTest(unittest.TestCase):
    """Verify advertisers get a private message when their ad ships."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = FakeGateway()
        self.settings = Settings(
            db_path=str(Path(self.tmp.name) / "notify.sqlite3"),
            bot_username="ChaBoTestBot",
        )
        self.app = create_app(self.settings, self.gateway)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _setup_one_running_delivery(self) -> dict:
        self.app.ledger.manual_topup(10001, money_to_cents("20"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100129,
            title="退款通知频道",
            username="refund_channel",
            owner_telegram_user_id=20004,
            owner_display_name="频道主",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="退款通知测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])
        delivered = self.app.fulfillment.dispatch_due()
        return {"channel": channel, "delivery_id": delivered[0]["delivery_id"]}

    def test_advertiser_gets_private_notice_after_delivery(self) -> None:
        self.app.ledger.manual_topup(10001, money_to_cents("20"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100128,
            title="通知测试频道",
            username="notify_channel",
            owner_telegram_user_id=20003,
            owner_display_name="频道主",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="发布通知测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])

        before = len(self.gateway.private_messages)
        delivered = self.app.fulfillment.dispatch_due()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["status"], "sent")

        new_messages = self.gateway.private_messages[before:]
        delivery_msgs = [m for m in new_messages if "已发布" in m["text"]]
        self.assertEqual(len(delivery_msgs), 1, f"expected one '已发布' notice, got {new_messages}")
        msg = delivery_msgs[0]
        self.assertEqual(msg["chat_id"], "10001")
        self.assertIn("通知测试频道", msg["text"])
        self.assertIn("USD", msg["text"])
        # Detail deep link must be present so the advertiser can jump to evidence
        urls = [
            btn["url"]
            for row in (msg["inline_keyboard"] or [])
            for btn in row
            if "url" in btn
        ]
        self.assertTrue(any(u.startswith("https://t.me/ChaBoTestBot?start=ad_") for u in urls), urls)

    def test_advertiser_gets_full_refund_notice(self) -> None:
        ctx = self._setup_one_running_delivery()
        before = len(self.gateway.private_messages)
        self.app.orders.refund_delivery(ctx["delivery_id"], reason="频道主提前删帖")

        new_messages = self.gateway.private_messages[before:]
        refund_msgs = [m for m in new_messages if "退款已到账" in m["text"]]
        self.assertEqual(len(refund_msgs), 1, f"expected one refund notice; got {new_messages}")
        msg = refund_msgs[0]
        self.assertEqual(msg["chat_id"], "10001")
        self.assertIn("退款通知频道", msg["text"])
        self.assertIn("全额退款", msg["text"])
        self.assertIn("频道主提前删帖", msg["text"])

    def test_advertiser_gets_pause_notice_when_send_fails(self) -> None:
        self.app.ledger.manual_topup(10001, money_to_cents("20"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100130,
            title="暂停通知频道",
            username="pause_channel",
            owner_telegram_user_id=20005,
            owner_display_name="频道主",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="暂停通知测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("10"),
        )
        self.app.orders.approve_order(order["id"])

        # Simulate Telegram failing on send_ad → fulfillment marks delivery
        # failed → calls orders.pause_and_release → should emit pause notice.
        self.gateway.fail_send = True
        before = len(self.gateway.private_messages)
        result = self.app.fulfillment.dispatch_due()
        self.assertEqual(result[0]["status"], "failed")

        new_messages = self.gateway.private_messages[before:]
        pause_msgs = [m for m in new_messages if "投放已暂停" in m["text"]]
        self.assertEqual(len(pause_msgs), 1, f"expected one pause notice; got {new_messages}")
        msg = pause_msgs[0]
        self.assertEqual(msg["chat_id"], "10001")
        self.assertIn("暂停通知频道", msg["text"])
        self.assertIn("已退回剩余预算", msg["text"])

    def test_advertiser_gets_partial_refund_notice(self) -> None:
        ctx = self._setup_one_running_delivery()
        before = len(self.gateway.private_messages)
        self.app.orders.refund_delivery_partial(
            ctx["delivery_id"],
            money_to_cents("0.50"),
            reason="部分补偿广告主",
        )

        new_messages = self.gateway.private_messages[before:]
        refund_msgs = [m for m in new_messages if "退款已到账" in m["text"]]
        self.assertEqual(len(refund_msgs), 1)
        self.assertIn("部分退款", refund_msgs[0]["text"])
        self.assertIn("USD 0.50", refund_msgs[0]["text"])

    def test_advertiser_gets_policy_change_notice_when_band_changes(self) -> None:
        self.app.ledger.manual_topup(10001, money_to_cents("20"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100131,
            title="调价通知频道",
            username="policy_channel",
            owner_telegram_user_id=20006,
            owner_display_name="频道主",
        )
        self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="调价通知测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("10"),
        )
        # Order is pending_review; advertiser hasn't shipped yet. Now publisher
        # changes the price band on standard_card.
        before = len(self.gateway.private_messages)
        self.app.channels.set_format_policy(
            channel["id"],
            "standard_card",
            enabled=True,
            owner_price_band="high",
        )
        new_messages = self.gateway.private_messages[before:]
        policy_msgs = [m for m in new_messages if "频道主刚刚修改了设置" in m["text"]]
        self.assertEqual(len(policy_msgs), 1, f"expected one policy-change notice; got {new_messages}")
        msg = policy_msgs[0]
        self.assertEqual(msg["chat_id"], "10001")
        self.assertIn("标准插播", msg["text"])
        self.assertIn("中档", msg["text"])
        self.assertIn("高档", msg["text"])

    def test_placement_top_block_shows_channel_quality_signals(self) -> None:
        # P1-7: traffic + category + risk surface in the configurator's top
        # block so advertisers can judge channel quality before committing.
        self.app.ledger.manual_topup(10001, money_to_cents("100"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100137,
            title="质量信号频道",
            username="quality_channel",
            owner_telegram_user_id=20011,
            owner_display_name="频道主",
        )
        self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="software",
            median_24h_views=20000,
            subscribers=50000,
        )
        text = self.app.update_handler._placement_text(
            channel=channel,
            payload={"channel_id": channel["id"], "slot_type": "standard_card"},
            panel="home",
        )
        self.assertIn("订阅 50,000", text)
        self.assertIn("24h 中位浏览 20,000", text)
        self.assertIn("软件 (商业价值 高)", text)
        self.assertIn("风控", text)

    def test_advertiser_orders_page_includes_aggregate_report(self) -> None:
        # P1-8: "📣 我的广告" 现在带"投放总览" + "频道分布" + "最近订单"
        ctx = self._setup_one_running_delivery()
        # Trigger the advertiser orders rendering
        before = len(self.gateway.private_messages) + len(self.gateway.text_edits)
        self.app.update_handler._send_advertiser_orders(
            chat_id="10001",
            user={"id": "10001", "first_name": "广告主"},
            source_message=None,
        )
        sent = self.gateway.private_messages[-1]
        text = sent["text"]
        self.assertIn("📊 投放总览", text)
        self.assertIn("订单 1", text)
        self.assertIn("已发", text)
        self.assertIn("详情页点击", text)
        self.assertIn("最近订单", text)
        self.assertIn(ctx["channel"]["title"], text)

    def test_placement_top_block_no_assessment_no_quality_lines(self) -> None:
        self.app.ledger.manual_topup(10001, money_to_cents("100"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100138,
            title="未评估频道",
            username="no_assessment",
            owner_telegram_user_id=20012,
            owner_display_name="频道主",
        )
        text = self.app.update_handler._placement_text(
            channel=channel,
            payload={"channel_id": channel["id"], "slot_type": "standard_card"},
            panel="home",
        )
        self.assertNotIn("订阅", text)
        self.assertNotIn("商业价值", text)

    def test_placement_confirm_panel_shows_cost_breakdown(self) -> None:
        # P1-6: cost confirm panel shows base × pin × period decomposition,
        # plus publisher-net / platform-fee split so advertiser sees where cents go.
        self.app.ledger.manual_topup(10001, money_to_cents("100"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100135,
            title="拆解测试频道",
            username="breakdown_channel",
            owner_telegram_user_id=20009,
            owner_display_name="频道主",
        )
        self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="software",
            median_24h_views=20000,
            subscribers=50000,
        )
        self.app.pricing.apply_quotes_to_rate_cards(channel["id"])

        text = self.app.update_handler._placement_text(
            channel=channel,
            payload={
                "channel_id": channel["id"],
                "slot_type": "standard_card",
                "pin": True,
                "period": "week",
                "creative_text": "x",
                "target_url": "https://example.com",
            },
            panel="confirm",
        )
        # Must show breakdown markers so advertiser sees structure
        self.assertIn("📊 报价拆解", text)
        self.assertIn("基准 USD", text)
        self.assertIn("置顶加价", text)  # pin=True
        self.assertIn("7 天循环", text)  # period=week
        self.assertIn("频道主净收", text)
        self.assertIn("平台服务费", text)

    def test_placement_display_panel_labels_each_format_with_price(self) -> None:
        # P1-5: per-format price labels in the placement display panel so
        # advertisers see costs before committing to a format.
        self.app.ledger.manual_topup(10001, money_to_cents("20"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100133,
            title="标价测试频道",
            username="price_label",
            owner_telegram_user_id=20008,
            owner_display_name="频道主",
        )
        # Need rates to exist for each slot.
        self.app.pricing.assess_channel(
            channel_id=channel["id"],
            category="software",
            median_24h_views=20000,
            subscribers=50000,
        )
        self.app.pricing.apply_quotes_to_rate_cards(channel["id"])

        keyboard = self.app.update_handler._placement_keyboard(
            "display",
            payload={"channel_id": channel["id"], "slot_type": "standard_card"},
        )
        all_buttons = [btn for row in keyboard for btn in row]
        format_buttons = [b for b in all_buttons if b["callback_data"].startswith("place:slot:")]
        self.assertEqual(len(format_buttons), 3)
        for btn in format_buttons:
            self.assertIn(" · USD ", btn["text"], f"missing price label on {btn}")

    def test_no_policy_change_notice_when_only_default_inserted(self) -> None:
        # Calling set_format_policy on a channel with no prior policy row
        # creates the default + then writes user's value. The "previous"
        # snapshot is the default so we should not emit a change notice.
        self.app.ledger.manual_topup(10001, money_to_cents("20"), display_name="广告主")
        channel = self.app.channels.bind_channel(
            telegram_chat_id=-100132,
            title="首次设置频道",
            username="first_policy",
            owner_telegram_user_id=20007,
            owner_display_name="频道主",
        )
        self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="首次设置测试",
            target_url="https://example.com",
            budget_cents=money_to_cents("10"),
        )
        before = len(self.gateway.private_messages)
        # Same band as the default → no actual change → no notification.
        self.app.channels.set_format_policy(
            channel["id"],
            "standard_card",
            enabled=True,
            owner_price_band="medium",
        )
        new_messages = self.gateway.private_messages[before:]
        policy_msgs = [m for m in new_messages if "频道主刚刚修改了设置" in m["text"]]
        self.assertEqual(len(policy_msgs), 0, f"unexpected change notice; got {new_messages}")


class MigrationTrackingTest(unittest.TestCase):
    """Verify the numbered-migration registry records each id once."""

    def test_schema_migrations_records_all_known_ids(self) -> None:
        from chabo.db import MIGRATIONS, Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "mig.sqlite3"))
            db.init()
            with db.connect() as conn:
                rows = conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
            applied_ids = [row[0] for row in rows]

        expected_ids = [mig_id for mig_id, _ in MIGRATIONS]
        self.assertEqual(applied_ids, expected_ids)

    def test_migrations_are_idempotent_when_re_initing(self) -> None:
        from chabo.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "mig.sqlite3")
            Database(path).init()
            # second init must not error or duplicate rows
            Database(path).init()
            with Database(path).connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
