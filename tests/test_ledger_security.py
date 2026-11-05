from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixtures import FakeGateway
from chabo.app import create_app
from chabo.config import Settings
from chabo.services import InvalidState


class TestLedgerSecuritySliceS02(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "s02.sqlite3")
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

    def test_manual_topup_rejects_zero_or_negative_amount(self) -> None:
        self.app.ledger.manual_topup(10001, 5000, display_name="广告主")
        with self.assertRaises(InvalidState):
            self.app.ledger.manual_topup(10001, 0)
        with self.assertRaises(InvalidState):
            self.app.ledger.manual_topup(10001, -2000)

        with self.app.db.connect() as conn:
            row = conn.execute(
                "SELECT available_balance_cents FROM accounts WHERE telegram_user_id = '10001'"
            ).fetchone()
            self.assertEqual(int(row["available_balance_cents"]), 5000)

    def test_concurrent_approve_topup_credits_only_once(self) -> None:
        self.app.ledger.manual_topup(33333, 1, display_name="申请人")
        self.app.ledger.manual_topup(44444, 1, display_name="审批人")
        req = self.app.topup_approvals.request_topup(
            recipient_telegram_user_id=10001,
            amount_cents=10000,
            reason="并发入账攻防测试",
            requester_telegram_user_id=33333,
        )

        barrier = threading.Barrier(4)
        successes: list[dict] = []
        failures: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                res = self.app.topup_approvals.approve_topup(
                    request_id=req["id"],
                    approver_telegram_user_id=44444,
                    approval_note="并发点击",
                )
                with lock:
                    successes.append(res)
            except Exception as exc:
                with lock:
                    failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 3)
        with self.app.db.connect() as conn:
            acc = conn.execute(
                "SELECT available_balance_cents FROM accounts WHERE telegram_user_id = '10001'"
            ).fetchone()
            self.assertEqual(int(acc["available_balance_cents"]), 10000)

    def test_paused_order_cannot_be_approved_without_reserved_budget(self) -> None:
        self.app.ledger.manual_topup(10001, 5000, display_name="广告主")
        channel = self.app.channels.bind_channel(
            owner_telegram_user_id=20001,
            owner_display_name="频道主",
            telegram_chat_id=-100123456,
            title="测试频道",
            username="test_chan",
        )
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10001,
            channel_token=channel["ref_token"],
            slot_type="standard",
            text="测试广告文案",
            target_url="https://example.com",
            budget_cents=2000,
        )
        self.assertEqual(order["status"], "pending_review")

        paused = self.app.orders.advertiser_pause_order(
            order_id=order["id"],
            advertiser_telegram_user_id=10001,
        )
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(int(paused["reserved_cents"]), 0)

        with self.assertRaises(InvalidState):
            self.app.orders.approve_order(order["id"])


if __name__ == "__main__":
    unittest.main()
