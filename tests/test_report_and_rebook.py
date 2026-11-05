from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from _fixtures import FakeGateway
from chabo.app import create_app
from chabo.config import Settings
from chabo.services._common import utcnow
from chabo.webapi.main import create_web_api


class ReportAndRebookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "chabo_g3.db")
        self.settings = Settings(
            db_path=self.db_path,
            bot_token="123456:TESTTOKEN",
            bot_username="chabo_test_bot",
            admin_token="test-admin-token",
            environment="test",
            dev_auth_bypass=True,
            dev_session_enabled=True,
        )
        self.gateway = FakeGateway()
        self.app = create_app(self.settings, gateway=self.gateway)
        self.api = create_web_api(self.settings, chabo=self.app)
        self.client = TestClient(self.api)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_report_by_channel_has_no_left_join_inflation_and_is_free_for_all(self) -> None:
        ch = self.app.channels.bind_channel(
            telegram_chat_id=-100801,
            title="防膨胀测试频道",
            username="ch_no_inflate",
            owner_telegram_user_id=20801,
            owner_display_name="频道主801",
        )
        self.app.ledger.manual_topup(10801, 50000, display_name="广告主801")
        now = utcnow()
        # Create 1 order with budget for 3 deliveries (default standard_card rate is 1000 cents)
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10801,
            channel_token=ch["ref_token"],
            slot_type="standard_card",
            text="多次发布插播测试",
            target_url="https://example.com/multi",
            budget_cents=3000,
            scheduled_at=now - timedelta(days=2),
            end_at=now + timedelta(days=5),
            frequency_per_day=2,
        )
        self.app.orders.approve_order(order["id"])

        # Dispatch 3 times across simulated schedule ticks
        for _ in range(3):
            with self.app.db.transaction() as conn:
                conn.execute(
                    "UPDATE deliveries SET scheduled_at = datetime('now', '-1 minute') WHERE order_id = ? AND status = 'scheduled'",
                    (order["id"],),
                )
            self.app.fulfillment.dispatch_due()

        rep = self.app.advertisers.report(10801)
        self.assertFalse(rep["limited"])
        self.assertEqual(rep["orders_count"], 1)
        self.assertEqual(rep["sent_count"], 3)
        self.assertEqual(rep["spent_cents"], 3000)
        self.assertEqual(len(rep["by_channel"]), 1)
        ch_row = rep["by_channel"][0]
        # Must NOT be 3 orders or 9000 spent_cents!
        self.assertEqual(ch_row["orders_count"], 1)
        self.assertEqual(ch_row["spent_cents"], 3000)
        self.assertEqual(ch_row["charged_cents"], 3000)

    def test_confidence_label_share_report_and_one_click_rebook(self) -> None:
        ch = self.app.channels.bind_channel(
            telegram_chat_id=-100802,
            title="高转化科技频道",
            username="ch_tech_802",
            owner_telegram_user_id=20802,
            owner_display_name="频道主802",
        )
        assessment = self.app.pricing.assess_channel(
            channel_id=ch["id"],
            category="software",
            median_24h_views=25000,
            subscribers=60000,
            light_unique_clickers_30d=250,
            repeat_purchase_count=4,
            risk_level="normal",
        )
        self.assertEqual(assessment["confidence_level"], "medium")
        self.assertEqual(assessment["confidence_badge"], "🛡️ 实单验证")
        self.assertIn(assessment["grade"], {"S", "A", "B", "C", "D"})
        self.assertGreaterEqual(assessment["verified_orders_count"], 4)

        self.app.ledger.manual_topup(10802, 50000, display_name="广告主802")
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10802,
            channel_token=ch["ref_token"],
            slot_type="standard_card",
            text="AI 开发者工具推广",
            target_url="https://example.com/ai-tool",
            budget_cents=5000,
        )
        self.app.orders.approve_order(order["id"])
        dispatched = self.app.fulfillment.dispatch_due()
        dlv_id = dispatched[0]["delivery_id"]

        self.app.fulfillment.sync_delivery_metrics(
            checkpoint_label="24h",
            views_by_delivery={dlv_id: 5000},
        )
        with self.app.db.transaction() as conn:
            conn.execute("UPDATE deliveries SET click_count = 25 WHERE id = ?", (dlv_id,))

        # Login as advertiser via dev-session
        login_res = self.client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "test-admin-token"},
            json={"telegram_user_id": "10802", "role": "advertiser", "portals": ["advertiser"]},
        )
        self.assertEqual(login_res.status_code, 200)

        # Fetch report + 4-quadrant recommendations
        rep_res = self.client.get("/api/advertiser/report")
        self.assertEqual(rep_res.status_code, 200)
        rep_data = rep_res.json()
        self.assertEqual(rep_data["impressions_count"], 5000)
        self.assertEqual(rep_data["clicks_count"], 25)
        self.assertEqual(len(rep_data["checkpoint_curve"]), 4)
        self.assertEqual(len(rep_data["recommendations"]), 1)
        self.assertEqual(rep_data["recommendations"][0]["action_type"], "scale_up")

        # Create public shareable report token and read without auth
        share_res = self.client.post("/api/advertiser/reports/share-token")
        self.assertEqual(share_res.status_code, 200)
        share_token = share_res.json()["share_token"]

        anon_client = TestClient(self.api)
        pub_res = anon_client.get(f"/api/public/reports/{share_token}")
        self.assertEqual(pub_res.status_code, 200)
        pub_data = pub_res.json()
        self.assertEqual(pub_data["impressions_count"], 5000)
        self.assertIn("双轨归因验真", pub_data["verification_watermark"])
        self.assertNotIn("reserved_cents", pub_data)

        # 1-Click Rebook
        rebook_res = self.client.post(
            "/api/advertiser/rebook",
            json={"source_order_id": order["id"], "budget_cents": 6000},
        )
        self.assertEqual(rebook_res.status_code, 200)
        rebooked = rebook_res.json()
        self.assertNotEqual(rebooked["id"], order["id"])
        self.assertEqual(rebooked["budget_cents"], 6000)

        # Advertiser self-service pause & refund reserved budget
        pause_res = self.client.post(f"/api/advertiser/orders/{rebooked['id']}/pause")
        self.assertEqual(pause_res.status_code, 200)
        paused_order = pause_res.json()
        self.assertEqual(paused_order["status"], "paused")
        self.assertEqual(paused_order["reserved_cents"], 0)

    def test_operator_reconcile_delivery_confirm_sent_and_confirm_failed(self) -> None:
        ch = self.app.channels.bind_channel(
            telegram_chat_id=-100803,
            title="异常对账测试频道",
            username="ch_reconcile_803",
            owner_telegram_user_id=20803,
            owner_display_name="频道主803",
        )
        self.app.ledger.manual_topup(10803, 50000, display_name="广告主803")
        order1 = self.app.orders.create_order(
            advertiser_telegram_user_id=10803,
            channel_token=ch["ref_token"],
            slot_type="standard_card",
            text="异常对账测试 1",
            target_url="https://example.com/rec1",
            budget_cents=2000,
        )
        self.app.orders.approve_order(order1["id"])
        order2 = self.app.orders.create_order(
            advertiser_telegram_user_id=10803,
            channel_token=ch["ref_token"],
            slot_type="standard_card",
            text="异常对账测试 2",
            target_url="https://example.com/rec2",
            budget_cents=2000,
        )
        self.app.orders.approve_order(order2["id"])

        with self.app.db.transaction() as conn:
            dlv1 = conn.execute("SELECT id FROM deliveries WHERE order_id = ?", (order1["id"],)).fetchone()["id"]
            dlv2 = conn.execute("SELECT id FROM deliveries WHERE order_id = ?", (order2["id"],)).fetchone()["id"]
            conn.execute(
                "UPDATE deliveries SET status = 'reconciliation_required', message_id = '9901' WHERE id = ?",
                (dlv1,),
            )
            conn.execute(
                "UPDATE deliveries SET status = 'reconciliation_required' WHERE id = ?",
                (dlv2,),
            )

        admin_client = TestClient(self.api)
        admin_client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "test-admin-token"},
            json={"telegram_user_id": "90803", "role": "admin", "portals": ["admin"]},
        )

        # 1) confirm_sent -> settles ledger & marks sent
        res1 = admin_client.post(
            f"/api/admin/deliveries/{dlv1}/reconcile",
            json={"resolution": "confirm_sent", "note": "人工核实频道已发出"},
        )
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["status"], "sent")
        self.assertEqual(res1.json()["charge_cents"], 1000)

        # 2) confirm_failed -> marks failed & releases order reserved budget
        res2 = admin_client.post(
            f"/api/admin/deliveries/{dlv2}/reconcile",
            json={"resolution": "confirm_failed", "note": "人工核实频道未发出"},
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["status"], "failed")
        with self.app.db.transaction() as conn:
            ord2_row = conn.execute("SELECT status, reserved_cents FROM ad_orders WHERE id = ?", (order2["id"],)).fetchone()
            self.assertEqual(ord2_row["status"], "paused")
            self.assertEqual(ord2_row["reserved_cents"], 0)


if __name__ == "__main__":
    unittest.main()
