from __future__ import annotations

import tempfile
import unittest
import contextlib
import hashlib
import hmac
import io
import json
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from chabo.app import create_app
from chabo.audit import AUDIT_HASH_VERSION, compute_audit_hash, insert_audit_log, verify_audit_chain
from chabo.cli import main as chabo_cli_main
from chabo.config import Settings
from chabo.dev_seed import seed_web_demo
from chabo.webapi.auth import grant_portal
from chabo.webapi.main import create_web_api


class WebApiFoundationTest(unittest.TestCase):
    def make_client(self) -> TestClient:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            db_path=str(Path(self.tmp.name) / "chabo.sqlite3"),
            admin_token="admin-secret-token",
            dev_session_enabled=True,
        )
        chabo = create_app(settings)
        return TestClient(create_web_api(settings, chabo))

    def make_telegram_client(self) -> TestClient:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            db_path=str(Path(self.tmp.name) / "chabo.sqlite3"),
            admin_token="admin-secret-token",
            dev_session_enabled=True,
            bot_token="123456:test_bot_token",
        )
        chabo = create_app(settings)
        return TestClient(create_web_api(settings, chabo))

    def test_health_and_dev_session_login(self) -> None:
        client = self.make_client()

        health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])

        unauth = client.get("/api/advertiser/dashboard")
        self.assertEqual(unauth.status_code, 401)

        denied = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "wrong"},
            json={"telegram_user_id": "10001", "portals": ["advertiser"]},
        )
        self.assertEqual(denied.status_code, 401)

        login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={
                "telegram_user_id": "10001",
                "display_name": "广告主A",
                "portals": ["admin", "advertiser"],
            },
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn("chabo_session", client.cookies)

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["account"]["telegram_user_id"], "10001")
        self.assertEqual(me.json()["portals"], ["admin", "advertiser"])

        advertiser = client.get("/api/advertiser/dashboard")
        self.assertEqual(advertiser.status_code, 200)
        self.assertIn("balance", advertiser.json())

        admin = client.get("/api/admin/summary")
        self.assertEqual(admin.status_code, 200)
        self.assertIn("pending_review_orders", admin.json()["metrics"])

        orders = client.get("/api/admin/orders?status=pending_review")
        self.assertEqual(orders.status_code, 200)
        self.assertEqual(orders.json()["items"], [])

        topups = client.get("/api/admin/topups?status=pending")
        self.assertEqual(topups.status_code, 200)
        self.assertEqual(topups.json()["items"], [])

        wallet = client.get("/api/admin/wallet")
        self.assertEqual(wallet.status_code, 200)
        self.assertIn("totals", wallet.json())
        self.assertIn("recent_ledger", wallet.json())

        settings = client.get("/api/admin/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("release_gates", settings.json())
        self.assertIn("audit_retention_days", settings.json())

        deliveries = client.get("/api/admin/deliveries")
        self.assertEqual(deliveries.status_code, 200)
        self.assertEqual(deliveries.json()["items"], [])

        disputes = client.get("/api/admin/disputes?status=open")
        self.assertEqual(disputes.status_code, 200)
        self.assertEqual(disputes.json()["items"], [])

    def test_production_web_security_settings(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            db_path=str(Path(self.tmp.name) / "chabo.sqlite3"),
            admin_token="admin-secret-token",
            dev_session_enabled=True,
            session_cookie_secure=True,
            session_cookie_samesite="strict",
            web_allowed_origins=("https://chabo.example",),
        )
        chabo = create_app(settings)
        client = TestClient(create_web_api(settings, chabo))

        preflight = client.options(
            "/api/auth/me",
            headers={
                "Origin": "https://chabo.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers["access-control-allow-origin"], "https://chabo.example")

        denied_origin = client.options(
            "/api/auth/me",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertNotIn("access-control-allow-origin", denied_origin.headers)

        login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "10009", "portals": ["advertiser"]},
        )
        self.assertEqual(login.status_code, 200)
        cookie = login.headers["set-cookie"].lower()
        self.assertIn("secure", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)

    def test_dev_auth_bypass_opens_all_portals_without_login(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            db_path=str(Path(self.tmp.name) / "chabo.sqlite3"),
            dev_auth_bypass=True,
            dev_auth_telegram_user_id="dev10001",
            dev_auth_display_name="本地开发者",
        )
        chabo = create_app(settings)
        client = TestClient(create_web_api(settings, chabo))

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["account"]["telegram_user_id"], "dev10001")
        self.assertEqual(me.json()["account"]["display_name"], "本地开发者")
        self.assertEqual(me.json()["portals"], ["admin", "advertiser", "publisher"])
        self.assertNotIn("chabo_session", client.cookies)

        advertiser = client.get("/api/advertiser/dashboard")
        self.assertEqual(advertiser.status_code, 200)

        admin = client.get("/api/admin/summary")
        self.assertEqual(admin.status_code, 200)

        publisher = client.get("/api/publisher/dashboard")
        self.assertEqual(publisher.status_code, 200)

    def test_web_demo_seed_prepares_three_portals(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            db_path=str(Path(self.tmp.name) / "chabo.sqlite3"),
            dev_auth_bypass=True,
            dev_auth_telegram_user_id="10001",
            dev_auth_display_name="H5 冒烟用户",
        )
        chabo = create_app(settings)
        seeded = seed_web_demo(chabo)
        self.assertEqual(seeded["account"]["portals"], ["admin", "advertiser", "publisher"])
        self.assertGreaterEqual(seeded["summary"]["channels"], 3)
        self.assertGreaterEqual(seeded["summary"]["materials"], 3)
        self.assertGreaterEqual(seeded["summary"]["pending_orders"], 3)
        self.assertGreaterEqual(seeded["summary"]["pending_topups"], 1)

        seeded_again = seed_web_demo(chabo)
        self.assertEqual(seeded_again["orders_created"], [])

        client = TestClient(create_web_api(settings, chabo))
        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["portals"], ["admin", "advertiser", "publisher"])

        admin = client.get("/api/admin/summary")
        self.assertEqual(admin.status_code, 200)
        self.assertGreaterEqual(admin.json()["metrics"]["pending_review_orders"], 3)

        advertiser = client.get("/api/advertiser/dashboard")
        self.assertEqual(advertiser.status_code, 200)
        self.assertGreaterEqual(advertiser.json()["orders"]["pending_orders"], 3)
        wallet = client.get("/api/advertiser/wallet")
        self.assertEqual(wallet.status_code, 200)
        self.assertIn("transactions", wallet.json())
        self.assertIn("reserved_orders", wallet.json())

        publisher = client.get("/api/publisher/dashboard")
        self.assertEqual(publisher.status_code, 200)
        self.assertGreaterEqual(publisher.json()["channels"], 3)
        earnings = client.get("/api/publisher/earnings")
        self.assertEqual(earnings.status_code, 200)
        self.assertGreaterEqual(len(earnings.json()["channels"]), 3)

    def test_verify_web_dry_run_lists_local_acceptance_steps(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = chabo_cli_main(["verify-web", "--dry-run", "--seed-demo", "--h5-smoke"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["ok"])
        step_names = [step["name"] for step in report["steps"]]
        self.assertEqual(
            step_names,
            [
                "python_compile",
                "python_tests",
                "frontend_build",
                "seed_web_demo",
                "preflight",
                "api_health",
                "h5_smoke",
            ],
        )
        preflight = next(step for step in report["steps"] if step["name"] == "preflight")
        self.assertIn("--allow-dev-auth-bypass", preflight["command"])
        self.assertIn("--allow-weak-tokens", preflight["command"])

    def test_verify_web_production_dry_run_keeps_strict_preflight(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = chabo_cli_main(
                [
                    "verify-web",
                    "--profile",
                    "production",
                    "--host",
                    "chabo.example",
                    "--health-url",
                    "https://chabo.example/api/health",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual(report["profile"], "production")
        self.assertEqual(report["health_url"], "https://chabo.example/api/health")
        step_names = [step["name"] for step in report["steps"]]
        self.assertEqual(step_names, ["python_compile", "python_tests", "frontend_build", "preflight", "api_health"])
        preflight = next(step for step in report["steps"] if step["name"] == "preflight")
        self.assertIn("--host", preflight["command"])
        self.assertIn("chabo.example", preflight["command"])
        self.assertNotIn("--allow-dev-auth-bypass", preflight["command"])
        self.assertNotIn("--allow-weak-tokens", preflight["command"])

    def test_verify_web_can_include_audit_chain_gate(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = chabo_cli_main(["verify-web", "--dry-run", "--audit-chain"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        step_names = [step["name"] for step in report["steps"]]
        self.assertIn("audit_chain", step_names)
        audit_step = next(step for step in report["steps"] if step["name"] == "audit_chain")
        self.assertIn("verify-audit-chain", audit_step["command"])

    def test_production_disables_dev_session_and_dev_bypass(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            environment="production",
            db_path=str(Path(self.tmp.name) / "chabo.sqlite3"),
            admin_token="admin-secret-token",
            dev_session_enabled=True,
            dev_auth_bypass=True,
            dev_auth_telegram_user_id="dev10001",
        )
        chabo = create_app(settings)
        client = TestClient(create_web_api(settings, chabo))

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 401)

        dev_login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "10002", "portals": ["admin"]},
        )
        self.assertEqual(dev_login.status_code, 404)

    def test_preflight_rejects_production_dev_switches(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {
            "CHABO_ENV": "production",
            "CHABO_DB_PATH": str(Path(self.tmp.name) / "chabo.sqlite3"),
            "CHABO_ADMIN_TOKEN": "strong-admin-token-123456",
            "CHABO_WEBHOOK_SECRET": "strong-webhook-secret-123456",
            "CHABO_PUBLIC_BASE_URL": "https://chabo.example",
            "CHABO_BOT_TOKEN": "",
            "CHABO_BOT_USERNAME": "",
            "CHABO_DEV_SESSION_ENABLED": "1",
            "CHABO_DEV_AUTH_BYPASS": "1",
        }
        output = io.StringIO()
        with patch.dict("os.environ", env, clear=False), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                chabo_cli_main(["preflight", "--host", "chabo.example"])
        self.assertEqual(raised.exception.code, 2)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertIn("CHABO_DEV_AUTH_BYPASS", "\n".join(report["issues"]))
        self.assertIn("CHABO_DEV_SESSION_ENABLED", "\n".join(report["issues"]))

    def test_portal_access_blocks_ungranted_portal(self) -> None:
        client = self.make_client()
        login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "10002", "portals": ["advertiser"]},
        )
        self.assertEqual(login.status_code, 200)

        publisher = client.get("/api/publisher/dashboard")
        self.assertEqual(publisher.status_code, 403)

    def test_magic_link_can_be_consumed_once(self) -> None:
        client = self.make_client()
        created = client.post(
            "/api/auth/magic-link",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={
                "telegram_user_id": "10003",
                "display_name": "Magic User",
                "portals": ["advertiser"],
            },
        )
        self.assertEqual(created.status_code, 200)
        token = created.json()["token"]

        consumed = client.post("/api/auth/magic/consume", json={"token": token})
        self.assertEqual(consumed.status_code, 200)
        self.assertEqual(consumed.json()["portals"], ["advertiser"])
        self.assertIn("chabo_session", client.cookies)

        reused = TestClient(client.app).post("/api/auth/magic/consume", json={"token": token})
        self.assertEqual(reused.status_code, 401)

    def test_telegram_webapp_init_data_login(self) -> None:
        client = self.make_telegram_client()
        chabo = client.app.state.chabo
        with chabo.db.transaction() as conn:
            account = chabo.ledger.accounts.get_or_create_by_telegram(conn, "4242", "advertiser", "tg_user")
            grant_portal(conn, account_id=account["id"], portal="advertiser", status="active", reason="test")

        init_data = _signed_init_data(
            "123456:test_bot_token",
            {"id": 4242, "username": "tg_user"},
        )
        login = client.post("/api/auth/telegram-webapp", json={"init_data": init_data})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["portals"], ["advertiser"])
        self.assertIn("chabo_session", client.cookies)

        bad = TestClient(client.app).post(
            "/api/auth/telegram-webapp",
            json={"init_data": init_data.replace("hash=", "hash=bad")},
        )
        self.assertEqual(bad.status_code, 401)

    def test_telegram_login_uses_candidate_then_auto_activates_advertiser(self) -> None:
        client = self.make_telegram_client()
        chabo = client.app.state.chabo
        init_data = _signed_init_data(
            "123456:test_bot_token",
            {"id": 5252, "username": "new_advertiser"},
        )

        login = client.post("/api/auth/telegram-webapp", json={"init_data": init_data})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["portals"], [])
        self.assertEqual(login.json()["portal_statuses"][0]["portal"], "advertiser")
        self.assertEqual(login.json()["portal_statuses"][0]["status"], "candidate")
        self.assertEqual(client.get("/api/advertiser/dashboard").status_code, 403)

        channel = chabo.channels.bind_channel("-10005", "激活广告主频道", "activate_ad", "62001")
        chabo.ledger.manual_topup("5252", 2_000, memo="广告主激活测试入账")
        order = chabo.orders.create_order(
            advertiser_telegram_user_id="5252",
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            budget_cents=1_000,
            text="激活广告主测试广告",
            target_url="https://example.com/ad-activate",
        )
        chabo.orders.approve_order(order["id"])

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["portals"], ["advertiser"])
        self.assertEqual(me.json()["portal_statuses"][0]["status"], "active")
        self.assertIn("advertiser", me.json()["activation"]["promoted"])
        self.assertEqual(client.get("/api/advertiser/dashboard").status_code, 200)

    def test_telegram_login_auto_activates_publisher_after_real_delivery(self) -> None:
        client = self.make_telegram_client()
        chabo = client.app.state.chabo
        channel = chabo.channels.bind_channel("-10006", "激活频道主频道", "activate_pub", "63001")

        init_data = _signed_init_data(
            "123456:test_bot_token",
            {"id": 63001, "username": "publisher_candidate"},
        )
        login = client.post("/api/auth/telegram-webapp", json={"init_data": init_data})
        self.assertEqual(login.status_code, 200)
        statuses = {row["portal"]: row["status"] for row in login.json()["portal_statuses"]}
        self.assertEqual(statuses["advertiser"], "candidate")
        self.assertEqual(statuses["publisher"], "candidate")
        self.assertEqual(client.get("/api/publisher/dashboard").status_code, 403)

        chabo.ledger.manual_topup("64001", 2_000, memo="频道主激活广告主入账")
        order = chabo.orders.create_order(
            advertiser_telegram_user_id="64001",
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            budget_cents=1_000,
            text="激活频道主测试广告",
            target_url="https://example.com/pub-activate",
        )
        chabo.orders.approve_order(order["id"])
        with chabo.db.transaction() as conn:
            delivery_id = conn.execute(
                "SELECT id FROM deliveries WHERE order_id = ?",
                (order["id"],),
            ).fetchone()["id"]
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'sent', charge_cents = 1000, publisher_net_cents = 900, sent_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (delivery_id,),
            )

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["portals"], ["publisher"])
        statuses = {row["portal"]: row["status"] for row in me.json()["portal_statuses"]}
        self.assertEqual(statuses["publisher"], "active")
        self.assertIn("publisher", me.json()["activation"]["promoted"])
        self.assertEqual(client.get("/api/publisher/dashboard").status_code, 200)

    def test_web_tables_are_created(self) -> None:
        client = self.make_client()
        api = client.app.state.chabo
        with api.db.transaction() as conn:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertIn("portal_access", names)
        self.assertIn("web_sessions", names)
        self.assertIn("placement_plans", names)
        self.assertIn("placement_plan_items", names)

    def test_advertiser_materials_and_plan_draft(self) -> None:
        client = self.make_client()
        api = client.app.state.chabo
        channel = api.channels.bind_channel("-10001", "测试频道", "test_chan", "20001")
        login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "30001", "portals": ["advertiser"]},
        )
        self.assertEqual(login.status_code, 200)
        api.ledger.manual_topup("30001", 5_000, memo="网页端计划测试入账")

        created = client.post(
            "/api/advertiser/materials",
            json={
                "format_type": "standard_card",
                "text": "测试广告文案",
                "target_url": "https://example.com",
                "button_text": "查看详情",
            },
        )
        self.assertEqual(created.status_code, 200)
        material_id = created.json()["id"]

        materials = client.get("/api/advertiser/materials")
        self.assertEqual(materials.status_code, 200)
        self.assertEqual(materials.json()["total"], 1)

        plan = client.post(
            "/api/advertiser/plans",
            json={"title": "第一批计划", "creative_id": material_id},
        )
        self.assertEqual(plan.status_code, 200)
        plan_id = plan.json()["id"]
        items = client.post(
            f"/api/advertiser/plans/{plan_id}/items",
            json={"channel_ids": [channel["id"]], "slot_type": "standard_card"},
        )
        self.assertEqual(items.status_code, 200)
        self.assertEqual(len(items.json()["items"]), 1)
        self.assertEqual(items.json()["items"][0]["status"], "valid")

        submitted = client.post(f"/api/advertiser/plans/{plan_id}/submit", json={})
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.json()["status"], "submitted")
        self.assertEqual(len(submitted.json()["created_orders"]), 1)

        orders = client.get("/api/advertiser/orders")
        self.assertEqual(orders.status_code, 200)
        self.assertEqual(orders.json()["total"], 1)

    def test_publisher_channel_management(self) -> None:
        client = self.make_client()
        api = client.app.state.chabo
        channel = api.channels.bind_channel("-10002", "频道主管理", "pub_chan", "40001")
        login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "40001", "portals": ["publisher"]},
        )
        self.assertEqual(login.status_code, 200)

        channels = client.get("/api/publisher/channels")
        self.assertEqual(channels.status_code, 200)
        self.assertEqual(channels.json()["total"], 1)

        detail = client.get(f"/api/publisher/channels/{channel['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("format_policies", detail.json())

        daily = client.patch(
            f"/api/publisher/channels/{channel['id']}/daily-limit",
            json={"daily_ad_limit": 5},
        )
        self.assertEqual(daily.status_code, 200)
        self.assertEqual(daily.json()["daily_ad_limit"], 5)

        policy = client.patch(
            f"/api/publisher/channels/{channel['id']}/format-policy",
            json={"format_type": "strong_post", "enabled": False},
        )
        self.assertEqual(policy.status_code, 200)
        self.assertEqual(policy.json()["enabled"], 0)

        rate = client.patch(
            f"/api/publisher/channels/{channel['id']}/rate",
            json={"slot_type": "standard_card", "unit_price_cents": 1234},
        )
        self.assertEqual(rate.status_code, 200)
        self.assertEqual(rate.json()["unit_price_cents"], 1234)

    def test_admin_details_search_audit_and_publisher_delivery_records(self) -> None:
        client = self.make_client()
        api = client.app.state.chabo
        channel = api.channels.bind_channel("-10003", "运营详情频道", "ops_chan", "41001")
        api.ledger.manual_topup("31001", 3_000, memo="网页端详情测试入账")
        order = api.orders.create_order(
            advertiser_telegram_user_id="31001",
            channel_token=channel["ref_token"],
            slot_type="standard_card",
            budget_cents=1_000,
            text="网页端详情测试广告",
            target_url="https://example.com/detail",
        )
        admin_login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "91001", "portals": ["admin"]},
        )
        self.assertEqual(admin_login.status_code, 200)

        approved = client.post(f"/api/admin/orders/{order['id']}/approve", json={"note": "详情测试审核"})
        self.assertEqual(approved.status_code, 200)
        delivery_id = api.orders.get_order_view(order["id"])["deliveries"][0]["id"]

        order_detail = client.get(f"/api/admin/orders/{order['id']}")
        self.assertEqual(order_detail.status_code, 200)
        self.assertEqual(order_detail.json()["order"]["id"], order["id"])
        self.assertTrue(order_detail.json()["timeline"])
        self.assertTrue(order_detail.json()["ledger_transactions"])

        delivery_detail = client.get(f"/api/admin/deliveries/{delivery_id}")
        self.assertEqual(delivery_detail.status_code, 200)
        self.assertEqual(delivery_detail.json()["delivery"]["id"], delivery_id)

        accounts = client.get("/api/admin/accounts?q=31001")
        self.assertEqual(accounts.status_code, 200)
        self.assertEqual(accounts.json()["total"], 1)

        channels = client.get("/api/admin/channels?q=运营详情")
        self.assertEqual(channels.status_code, 200)
        self.assertEqual(channels.json()["items"][0]["id"], channel["id"])

        audit = client.get(f"/api/admin/audit-logs?q={order['id']}")
        self.assertEqual(audit.status_code, 200)
        self.assertTrue(any(item["action"] == "order_approved" for item in audit.json()["items"]))

        publisher_client = TestClient(client.app)
        publisher_login = publisher_client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "41001", "portals": ["publisher"]},
        )
        self.assertEqual(publisher_login.status_code, 200)
        publisher_deliveries = publisher_client.get(f"/api/publisher/channels/{channel['id']}/deliveries")
        self.assertEqual(publisher_deliveries.status_code, 200)
        self.assertEqual(publisher_deliveries.json()["items"][0]["id"], delivery_id)

    def test_admin_can_request_and_second_admin_can_approve_topup(self) -> None:
        client = self.make_client()
        requester = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "92001", "portals": ["admin"]},
        )
        self.assertEqual(requester.status_code, 200)
        created = client.post(
            "/api/admin/topups",
            json={
                "recipient_telegram_user_id": "32001",
                "amount_cents": 2500,
                "reason": "网页端人工入账测试",
                "request_note": "收款凭证已核对",
            },
        )
        self.assertEqual(created.status_code, 200)
        request_id = created.json()["id"]

        self_approve = client.post(f"/api/admin/topups/{request_id}/approve", json={"note": "同人审批"})
        self.assertEqual(self_approve.status_code, 400)

        approver_client = TestClient(client.app)
        approver = approver_client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "92002", "portals": ["admin"]},
        )
        self.assertEqual(approver.status_code, 200)
        approved = approver_client.post(f"/api/admin/topups/{request_id}/approve", json={"note": "二人复核通过"})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["status"], "approved")

        accounts = approver_client.get("/api/admin/accounts?q=32001")
        self.assertEqual(accounts.status_code, 200)
        self.assertEqual(accounts.json()["items"][0]["available_balance_cents"], 2500)

    def test_admin_level_blocks_sensitive_actions(self) -> None:
        client = self.make_client()
        login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "93001", "portals": ["admin"]},
        )
        self.assertEqual(login.status_code, 200)
        chabo = client.app.state.chabo
        with chabo.db.transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '93001'").fetchone()
            conn.execute(
                "UPDATE portal_access SET metadata_json = ? WHERE account_id = ? AND portal = 'admin'",
                (json.dumps({"admin_level": "viewer"}), account["id"]),
            )

        summary = client.get("/api/admin/summary")
        self.assertEqual(summary.status_code, 200)
        me = client.get("/api/auth/me")
        self.assertEqual(me.json()["admin_level"], "viewer")

        topup = client.post(
            "/api/admin/topups",
            json={
                "recipient_telegram_user_id": "32002",
                "amount_cents": 1000,
                "reason": "viewer 不应能入账",
            },
        )
        self.assertEqual(topup.status_code, 403)
        self.assertEqual(topup.json()["detail"], "admin_finance_required")

        wallet = client.get("/api/admin/wallet")
        self.assertEqual(wallet.status_code, 403)
        self.assertEqual(wallet.json()["detail"], "admin_finance_required")

        settings = client.get("/api/admin/settings")
        self.assertEqual(settings.status_code, 200)

        audit_export = client.get("/api/admin/audit-logs/export.csv")
        self.assertEqual(audit_export.status_code, 403)
        self.assertEqual(audit_export.json()["detail"], "admin_super_admin_required")

        with chabo.db.transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = '93001'").fetchone()
            conn.execute(
                "UPDATE portal_access SET metadata_json = ? WHERE account_id = ? AND portal = 'admin'",
                (json.dumps({"admin_level": "finance"}), account["id"]),
            )

        finance_me = client.get("/api/auth/me")
        self.assertEqual(finance_me.json()["admin_level"], "finance")
        finance_wallet = client.get("/api/admin/wallet")
        self.assertEqual(finance_wallet.status_code, 200)
        finance_topup = client.post(
            "/api/admin/topups",
            json={
                "recipient_telegram_user_id": "32003",
                "amount_cents": 1000,
                "reason": "finance 可以提交入账申请",
            },
        )
        self.assertEqual(finance_topup.status_code, 200)
        finance_export = client.get("/api/admin/audit-logs/export.csv")
        self.assertEqual(finance_export.status_code, 403)
        self.assertEqual(finance_export.json()["detail"], "admin_super_admin_required")

    def test_super_admin_can_impersonate_and_return_to_admin(self) -> None:
        client = self.make_client()
        admin_login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "94001", "portals": ["admin"]},
        )
        self.assertEqual(admin_login.status_code, 200)
        chabo = client.app.state.chabo
        admin_id = admin_login.json()["account"]["id"]
        with chabo.db.transaction() as conn:
            target = chabo.ledger.accounts.get_or_create_by_telegram(conn, "94002", "advertiser", "被代看广告主")
            grant_portal(conn, account_id=target["id"], portal="advertiser", status="active", reason="impersonation_test")
            target_id = target["id"]

        missing_reason = client.post(
            "/api/admin/impersonations",
            json={
                "target_account_id": target_id,
                "portal": "advertiser",
                "reason": "   ",
            },
        )
        self.assertEqual(missing_reason.status_code, 400)

        started = client.post(
            "/api/admin/impersonations",
            json={
                "target_account_id": target_id,
                "portal": "advertiser",
                "reason": "测试代看",
            },
        )
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["impersonation"]["admin_account_id"], admin_id)

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["account"]["id"], target_id)
        self.assertEqual(me.json()["impersonator_account_id"], admin_id)
        self.assertEqual(me.json()["portals"], ["advertiser"])
        self.assertIn("session_expires_at", me.json())
        self.assertEqual(me.json()["session_expires_at"], started.json()["session_expires_at"])
        self.assertEqual(client.get("/api/advertiser/dashboard").status_code, 200)
        self.assertEqual(client.get("/api/admin/summary").status_code, 403)

        stopped = client.post("/api/auth/impersonation/stop")
        self.assertEqual(stopped.status_code, 200)
        audit = client.get(f"/api/admin/audit-logs?category=impersonation&q={target_id}")
        self.assertEqual(audit.status_code, 200)
        actions = {item["action"] for item in audit.json()["items"]}
        self.assertIn("admin_impersonation_started", actions)
        self.assertIn("admin_impersonation_stopped", actions)
        restored = client.get("/api/auth/me")
        self.assertEqual(restored.json()["account"]["id"], admin_id)
        self.assertEqual(restored.json()["impersonator_account_id"], None)
        self.assertIn("admin", restored.json()["portals"])
        self.assertEqual(client.get("/api/admin/summary").status_code, 200)

    def test_audit_chain_verification_reports_tampering(self) -> None:
        client = self.make_client()
        admin_login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "94001", "portals": ["admin"]},
        )
        self.assertEqual(admin_login.status_code, 200)
        admin_id = admin_login.json()["account"]["id"]
        chabo = client.app.state.chabo
        with chabo.db.transaction() as conn:
            first = insert_audit_log(
                conn,
                actor_account_id=admin_id,
                action="audit_verify_fixture_first",
                entity_type="account",
                entity_id=admin_id,
                payload={"step": 1},
            )
            insert_audit_log(
                conn,
                actor_account_id=admin_id,
                action="audit_verify_fixture_second",
                entity_type="account",
                entity_id=admin_id,
                payload={"step": 2},
            )
            direct_report = verify_audit_chain(conn)
        self.assertTrue(direct_report["ok"])
        self.assertEqual(direct_report["signed_rows"], 2)

        api_report = client.get("/api/admin/audit-logs/verify")
        self.assertEqual(api_report.status_code, 200)
        self.assertTrue(api_report.json()["ok"])
        self.assertEqual(api_report.json()["chain_head"], direct_report["chain_head"])

        ranged_report = client.get(
            "/api/admin/audit-logs/verify",
            params={
                "created_from": "2000-01-01 00:00:00",
                "created_to": "2999-12-31 23:59:59",
                "strict_unsigned": "true",
            },
        )
        self.assertEqual(ranged_report.status_code, 200)
        self.assertTrue(ranged_report.json()["ok"])
        self.assertEqual(ranged_report.json()["created_from"], "2000-01-01 00:00:00")
        self.assertEqual(ranged_report.json()["created_to"], "2999-12-31 23:59:59")

        output = io.StringIO()
        with patch.dict("os.environ", {"CHABO_DB_PATH": chabo.settings.db_path}, clear=False), contextlib.redirect_stdout(output):
            code = chabo_cli_main(["verify-audit-chain"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])

        with chabo.db.transaction() as conn:
            conn.execute(
                "UPDATE audit_logs SET payload_json = ? WHERE id = ?",
                (json.dumps({"step": "tampered"}, ensure_ascii=False), first["id"]),
            )
            tampered = verify_audit_chain(conn)
        self.assertFalse(tampered["ok"])
        self.assertGreaterEqual(tampered["invalid_hashes"], 1)

        tampered_api = client.get("/api/admin/audit-logs/verify")
        self.assertEqual(tampered_api.status_code, 200)
        self.assertFalse(tampered_api.json()["ok"])

    def test_super_admin_can_manually_grant_revoke_portals_and_admin_levels(self) -> None:
        client = self.make_client()
        admin_login = client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "95001", "portals": ["admin"]},
        )
        self.assertEqual(admin_login.status_code, 200)
        admin_id = admin_login.json()["account"]["id"]
        chabo = client.app.state.chabo
        with chabo.db.transaction() as conn:
            target = chabo.ledger.accounts.get_or_create_by_telegram(conn, "95002", "mixed", "手工权限用户")
            target_id = target["id"]

        granted = client.put(
            f"/api/admin/accounts/{target_id}/portals/advertiser",
            json={"status": "active", "reason": "手工开通广告主端"},
        )
        self.assertEqual(granted.status_code, 200)
        self.assertEqual(granted.json()["portal_access"]["status"], "active")

        admin_level_update = client.put(
            f"/api/admin/accounts/{target_id}/portals/admin",
            json={
                "status": "active",
                "admin_level": "finance",
                "reason": "设置财务管理员",
                "confirm_phrase": "确认调整权限",
            },
        )
        self.assertEqual(admin_level_update.status_code, 200)
        self.assertEqual(admin_level_update.json()["admin_level"], "finance")

        accounts = client.get("/api/admin/accounts?q=95002")
        self.assertEqual(accounts.status_code, 200)
        account_row = accounts.json()["items"][0]
        self.assertEqual(account_row["advertiser_portal_status"], "active")
        self.assertEqual(account_row["admin_level"], "finance")
        permission_audit = client.get(f"/api/admin/audit-logs?category=permission&q={target_id}")
        self.assertEqual(permission_audit.status_code, 200)
        permission_items = permission_audit.json()["items"]
        self.assertTrue(all(item["action"] == "admin_portal_access_updated" for item in permission_items))
        self.assertTrue(all(item["hash_version"] == AUDIT_HASH_VERSION for item in permission_items))
        self.assertTrue(all(len(item["audit_hash"]) == 64 for item in permission_items))
        self.assertTrue(any(item["previous_hash"] for item in permission_items))
        sample_audit = permission_items[0]
        self.assertEqual(
            sample_audit["audit_hash"],
            compute_audit_hash(
                audit_id=sample_audit["id"],
                actor_account_id=sample_audit["actor_account_id"],
                action=sample_audit["action"],
                entity_type=sample_audit["entity_type"],
                entity_id=sample_audit["entity_id"],
                payload=sample_audit["payload_json"],
                created_at=sample_audit["created_at"],
                previous_hash=sample_audit["previous_hash"],
            ),
        )
        actor_audit = client.get(f"/api/admin/audit-logs?category=permission&actor=95001&target=95002&created_from=2000-01-01&created_to=2999-12-31")
        self.assertEqual(actor_audit.status_code, 200)
        self.assertGreaterEqual(actor_audit.json()["total"], 2)
        self.assertTrue(all(item["actor_telegram_user_id"] == "95001" for item in actor_audit.json()["items"]))
        self.assertTrue(all(item["target_telegram_user_id"] == "95002" for item in actor_audit.json()["items"]))
        level_audit = client.get(f"/api/admin/audit-logs?category=admin_level&q={target_id}")
        self.assertEqual(level_audit.status_code, 200)
        level_payloads = [json.loads(item["payload_json"]) for item in level_audit.json()["items"]]
        self.assertTrue(all(item["portal"] == "admin" for item in level_payloads))
        self.assertTrue(any(item["admin_level"] == "finance" for item in level_payloads))
        level_payload = json.loads(level_audit.json()["items"][0]["payload_json"])
        self.assertIn("before", level_payload)
        self.assertIn("after", level_payload)
        self.assertIn("admin_level", level_payload["changed_fields"])
        exported = client.get(f"/api/admin/audit-logs/export.csv?category=permission&target={target_id}")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("text/csv", exported.headers["content-type"])
        self.assertIn("admin_portal_access_updated", exported.text)
        self.assertIn(target_id, exported.text)
        export_sha256 = exported.headers["x-chabo-audit-export-sha256"]
        self.assertEqual(len(export_sha256), 64)
        self.assertEqual(exported.headers["x-chabo-audit-chain-head"], sample_audit["audit_hash"])
        self.assertEqual(
            hashlib.sha256(exported.text.removeprefix("\ufeff").encode("utf-8")).hexdigest(),
            export_sha256,
        )
        export_audit = client.get("/api/admin/audit-logs?q=admin_audit_exported")
        self.assertEqual(export_audit.status_code, 200)
        export_payload = json.loads(export_audit.json()["items"][0]["payload_json"])
        self.assertEqual(export_payload["csv_sha256"], export_sha256)
        self.assertEqual(export_payload["chain_head_before_export"], sample_audit["audit_hash"])

        viewer_client = TestClient(client.app)
        viewer_login = viewer_client.post(
            "/api/auth/dev-session",
            headers={"X-Chabo-Admin-Token": "admin-secret-token"},
            json={"telegram_user_id": "95003", "portals": ["admin"]},
        )
        self.assertEqual(viewer_login.status_code, 200)
        viewer_id = viewer_login.json()["account"]["id"]
        set_viewer = client.put(
            f"/api/admin/accounts/{viewer_id}/portals/admin",
            json={
                "status": "active",
                "admin_level": "viewer",
                "reason": "设置只读管理员用于导出权限测试",
                "confirm_phrase": "确认调整权限",
            },
        )
        self.assertEqual(set_viewer.status_code, 200)
        forbidden_export = viewer_client.get("/api/admin/audit-logs/export.csv?category=permission")
        self.assertEqual(forbidden_export.status_code, 403)

        missing_reason = client.put(
            f"/api/admin/accounts/{target_id}/portals/publisher",
            json={"status": "active", "reason": "   "},
        )
        self.assertEqual(missing_reason.status_code, 400)
        self.assertEqual(missing_reason.json()["detail"], "permission_reason_required")

        revoked = client.put(
            f"/api/admin/accounts/{target_id}/portals/advertiser",
            json={"status": "revoked", "reason": "撤销广告主端", "confirm_phrase": "确认调整权限"},
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["portal_access"]["status"], "revoked")
        self.assertEqual(
            {row["portal"]: row["status"] for row in revoked.json()["portal_statuses"]}["advertiser"],
            "revoked",
        )

        self_downgrade = client.put(
            f"/api/admin/accounts/{admin_id}/portals/admin",
            json={
                "status": "active",
                "admin_level": "viewer",
                "reason": "不能自降级",
                "confirm_phrase": "确认调整权限",
            },
        )
        self.assertEqual(self_downgrade.status_code, 400)


if __name__ == "__main__":
    unittest.main()


def _signed_init_data(bot_token: str, user: dict) -> str:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("query_id", "test-query"),
        ("user", json.dumps(user, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    digest = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode([*pairs, ("hash", digest)])
