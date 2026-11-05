from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from _fixtures import FakeGateway
from chabo.app import create_app
from chabo.config import Settings
from chabo.telegram_abc_client import sync_channels_from_abc
from chabo.webapi.main import create_web_api
from chabo.webapi.plans import add_plan_items, create_plan, submit_plan
from chabo.webapi.scheduler import run_periodic_jobs_once


class PlansAndAttributionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "chabo_g2.db")
        self.settings = Settings(
            db_path=self.db_path,
            bot_token="123456:TESTTOKEN",
            bot_username="chabo_test_bot",
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

    def test_submit_plan_shares_single_campaign_and_honors_pin_enabled(self) -> None:
        ch1 = self.app.channels.bind_channel(
            telegram_chat_id=-100701,
            title="频道1",
            username="ch_g2_1",
            owner_telegram_user_id=20701,
            owner_display_name="频道主1",
        )
        ch2 = self.app.channels.bind_channel(
            telegram_chat_id=-100702,
            title="频道2",
            username="ch_g2_2",
            owner_telegram_user_id=20702,
            owner_display_name="频道主2",
        )
        self.app.ledger.manual_topup(10701, 20000, display_name="广告主")
        mat = self.app.materials.create_material(
            advertiser_telegram_user_id=10701,
            format_type="standard_card",
            text="测试批量计划文案",
            target_url="https://example.com/offer",
        )
        with self.app.db.transaction() as conn:
            adv = conn.execute("SELECT id FROM accounts WHERE telegram_user_id = '10701'").fetchone()
            adv_id = adv["id"]

        plan = create_plan(self.app, advertiser_account_id=adv_id, title="春季大促", creative_id=mat["id"])
        add_plan_items(
            self.app,
            advertiser_account_id=adv_id,
            plan_id=plan["id"],
            channel_ids=[ch1["id"], ch2["id"]],
            slot_type="standard_card",
            pin_enabled=True,
        )
        res = submit_plan(
            self.app,
            advertiser_account_id=adv_id,
            advertiser_telegram_user_id="10701",
            plan_id=plan["id"],
        )
        self.assertEqual(len(res["created_orders"]), 2)
        campaign_ids = {o["campaign_id"] for o in res["created_orders"]}
        self.assertEqual(len(campaign_ids), 1)
        with self.app.db.transaction() as conn:
            for o in res["created_orders"]:
                slot = conn.execute("SELECT slot_type FROM ad_slots WHERE id = ?", (o["slot_id"],)).fetchone()
                self.assertEqual(slot["slot_type"], "pin24h")

    def test_redirect_anti_fraud_filters_prefetch_and_deduplicates_clicks(self) -> None:
        ch = self.app.channels.bind_channel(
            telegram_chat_id=-100703,
            title="归因频道",
            username="ch_g2_attr",
            owner_telegram_user_id=20703,
            owner_display_name="频道主3",
        )
        self.app.ledger.manual_topup(10703, 10000, display_name="广告主3")
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10703,
            channel_token=ch["ref_token"],
            slot_type="standard_card",
            text="落地页测试广告",
            target_url="https://example.com/landing-g2",
            budget_cents=5000,
        )
        self.app.orders.approve_order(order["id"])
        dispatched = self.app.fulfillment.dispatch_due()
        self.assertEqual(len(dispatched), 1)
        delivery_id = dispatched[0]["delivery_id"]

        with self.app.db.transaction() as conn:
            dlv = conn.execute("SELECT tracking_token, click_count FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            token = dlv["tracking_token"]
            self.assertTrue(token.startswith("trk_"))

        # 1. Telegram link-preview prefetcher must NOT count as human click
        r_bot = self.client.get(
            f"/r/{token}",
            headers={"User-Agent": "TelegramBot (like TwitterBot)"},
            follow_redirects=False,
        )
        self.assertEqual(r_bot.status_code, 302)
        with self.app.db.transaction() as conn:
            dlv_after_bot = conn.execute("SELECT click_count FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            self.assertEqual(dlv_after_bot["click_count"], 0)

        # 2. Real human browser UA counts as 1 unique click
        human_headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
            "X-Forwarded-For": "203.0.113.42",
        }
        r_human_1 = self.client.get(f"/r/{token}", headers=human_headers, follow_redirects=False)
        self.assertEqual(r_human_1.status_code, 302)
        self.assertEqual(r_human_1.headers["location"], "https://example.com/landing-g2")

        # 3. Duplicate click from same IP + UA within 5 minutes is deduplicated
        r_human_2 = self.client.get(f"/r/{token}", headers=human_headers, follow_redirects=False)
        self.assertEqual(r_human_2.status_code, 302)

        with self.app.db.transaction() as conn:
            dlv_after_human = conn.execute("SELECT click_count FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            self.assertEqual(dlv_after_human["click_count"], 1)

    def test_sync_delivery_metrics_and_unpublish_due(self) -> None:
        ch = self.app.channels.bind_channel(
            telegram_chat_id=-100704,
            title="置顶与曝光频道",
            username="ch_g2_pin",
            owner_telegram_user_id=20704,
            owner_display_name="频道主4",
        )
        self.app.ledger.manual_topup(10704, 15000, display_name="广告主4")
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10704,
            channel_token=ch["ref_token"],
            slot_type="pin24h",
            text="置顶插播广告",
            target_url="https://example.com/pin",
            budget_cents=5000,
        )
        self.app.orders.approve_order(order["id"])
        dispatched = self.app.fulfillment.dispatch_due()
        delivery_id = dispatched[0]["delivery_id"]

        synced = self.app.fulfillment.sync_delivery_metrics(
            checkpoint_label="24h",
            views_by_delivery={delivery_id: 4200},
        )
        self.assertEqual(synced, 1)

        with self.app.db.transaction() as conn:
            conn.execute(
                "UPDATE deliveries SET sent_at = datetime('now', '-25 hours') WHERE id = ?",
                (delivery_id,),
            )
        unpinned = self.app.fulfillment.unpublish_due(pin_hours=24)
        self.assertEqual(unpinned, 1)

        with self.app.db.transaction() as conn:
            dlv = conn.execute("SELECT pinned, impression_count FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            self.assertEqual(dlv["pinned"], 0)
            self.assertEqual(dlv["impression_count"], 4200)

    def test_abc_channel_sync_probe_writeback_and_scheduler(self) -> None:
        synced = sync_channels_from_abc(
            self.app,
            owner_telegram_user_id=20705,
            channels_override=[
                {
                    "telegram_chat_id": "-100705",
                    "title": "TelegramABC 同步频道",
                    "username": "abc_synced_ch",
                    "subscribers": 45000,
                    "median_24h_views": 12000,
                    "category": "software",
                }
            ],
        )
        self.assertEqual(len(synced), 1)
        ch = synced[0]

        self.app.ledger.manual_topup(10705, 10000, display_name="广告主5")
        order = self.app.orders.create_order(
            advertiser_telegram_user_id=10705,
            channel_token=ch["ref_token"],
            slot_type="standard_card",
            text="探针回写测试",
            target_url="https://t.me/some_channel",
            budget_cents=3000,
        )
        self.app.orders.approve_order(order["id"])
        tick = run_periodic_jobs_once(self.app)
        self.assertGreaterEqual(tick["dispatched_count"], 1)

        probe = self.app.light_probes.create_probe(
            channel_id=ch["id"],
            short_text="了解详情",
            detail_text="探针详情",
            target_url="https://t.me/some_channel",
        )
        with self.app.db.transaction() as conn:
            self.app.light_probes.record_click(conn, probe_id=probe["id"], telegram_user_id="88001")
            # Duplicate click within 5 minutes must not double-increment click_count
            self.app.light_probes.record_click(conn, probe_id=probe["id"], telegram_user_id="88001")
            dlv = conn.execute("SELECT click_count FROM deliveries WHERE order_id = ?", (order["id"],)).fetchone()
            self.assertEqual(dlv["click_count"], 1)


if __name__ == "__main__":
    unittest.main()
