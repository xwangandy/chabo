from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .app import create_app
from .config import Settings
from .db import Database
from .money import cents_to_money, money_to_cents
from .services import NotFound


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def cmd_init_db(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    Database(settings.db_path).init()
    print(f"插播数据库已初始化：{settings.db_path}")


def cmd_seed_web_demo(args: argparse.Namespace) -> None:
    from .dev_seed import seed_web_demo

    app = create_app()
    print_json(
        seed_web_demo(
            app,
            telegram_user_id=args.telegram_user_id,
            display_name=args.display_name,
            min_pending_orders=args.pending_orders,
        )
    )


def cmd_verify_web(args: argparse.Namespace) -> None:
    if args.profile == "production" and args.seed_demo:
        raise ValueError("--seed-demo 只能用于 local profile，不能用于 production 验收")

    root = Path(__file__).resolve().parents[2]
    web_dir = root / "web"
    steps: list[dict[str, Any]] = []
    health_url = args.health_url or "http://127.0.0.1:8081/api/health"

    if not args.skip_tests:
        _verify_command(
            steps,
            "python_compile",
            [sys.executable, "-m", "compileall", "-q", "src", "tests"],
            cwd=root,
            dry_run=args.dry_run,
        )
        _verify_command(
            steps,
            "python_tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=root,
            dry_run=args.dry_run,
        )
    if not args.skip_build:
        _verify_command(
            steps,
            "frontend_build",
            ["npm", "run", "build"],
            cwd=web_dir,
            dry_run=args.dry_run,
        )
    if args.seed_demo:
        _verify_command(
            steps,
            "seed_web_demo",
            [
                sys.executable,
                "-m",
                "chabo.cli",
                "seed-web-demo",
                "--telegram-user-id",
                args.seed_telegram_user_id,
                "--display-name",
                args.seed_display_name,
                "--pending-orders",
                str(args.seed_pending_orders),
            ],
            cwd=root,
            dry_run=args.dry_run,
        )
    if not args.skip_preflight:
        preflight_cmd = [sys.executable, "-m", "chabo.cli", "preflight", "--host", args.host]
        if args.profile == "local" or args.allow_weak_tokens:
            preflight_cmd.append("--allow-weak-tokens")
        if args.profile == "local" or args.allow_dev_auth_bypass:
            preflight_cmd.append("--allow-dev-auth-bypass")
        _verify_command(
            steps,
            "preflight",
            preflight_cmd,
            cwd=root,
            dry_run=args.dry_run,
        )
    if args.audit_chain:
        audit_cmd = [sys.executable, "-m", "chabo.cli", "verify-audit-chain"]
        if args.strict_audit_chain:
            audit_cmd.append("--strict")
        _verify_command(
            steps,
            "audit_chain",
            audit_cmd,
            cwd=root,
            dry_run=args.dry_run,
        )
    if not args.skip_health:
        _verify_action(
            steps,
            "api_health",
            {"url": health_url},
            lambda: _read_health(health_url),
            dry_run=args.dry_run,
        )
    if args.h5_smoke:
        smoke_env = os.environ.copy()
        if args.profile == "local" or args.h5_smoke_auth_bypass:
            smoke_env["CHABO_H5_SMOKE_AUTH_BYPASS"] = "1"
        if args.h5_smoke_base_url:
            smoke_env["CHABO_H5_SMOKE_BASE_URL"] = args.h5_smoke_base_url
        if args.h5_smoke_api_url:
            smoke_env["CHABO_H5_SMOKE_API_URL"] = args.h5_smoke_api_url
        _verify_command(
            steps,
            "h5_smoke",
            ["npm", "run", "smoke:h5"],
            cwd=web_dir,
            env=smoke_env,
            dry_run=args.dry_run,
        )

    ok = all(step["status"] in {"passed", "planned"} for step in steps)
    report = {
        "ok": ok,
        "profile": args.profile,
        "dry_run": args.dry_run,
        "health_url": health_url,
        "steps": steps,
    }
    print_json(report)
    if not ok:
        sys.exit(2)


def cmd_verify_audit_chain(args: argparse.Namespace) -> None:
    from .audit import verify_audit_chain

    settings = Settings.from_env()
    app = create_app(settings)
    with app.db.transaction() as conn:
        report = verify_audit_chain(
            conn,
            created_from=args.created_from,
            created_to=args.created_to,
            strict_unsigned=args.strict,
        )
    print_json(report)
    if not report["ok"]:
        sys.exit(2)


def cmd_preflight(args: argparse.Namespace) -> None:
    """Pre-launch check for production deployments.

    Prints a structured JSON report of: DB ping, schema migration ok,
    backup-dir writability, token strength, and the same /health-style
    operational counters. Exits non-zero if any critical check fails so
    deploy scripts can gate the launch.
    """
    from .web import check_token_strength

    settings = Settings.from_env()
    report: dict[str, Any] = {
        "ok": True,
        "issues": [],
        "warnings": [],
        "checks": {},
    }

    # DB connect + migrate
    try:
        db = Database(settings.db_path)
        db.init()
        with db.connect() as conn:
            conn.execute("SELECT 1").fetchone()
            cols = [row[1] for row in conn.execute("PRAGMA table_info(creatives)").fetchall()]
            schema_ok = all(name in cols for name in ("advertiser_account_id", "format_type", "light_short_text", "archived_at"))
        report["checks"]["db"] = {"ok": True, "schema_up_to_date": schema_ok}
        if not schema_ok:
            report["issues"].append("creatives 表缺列；服务可能未初始化最新 schema")
            report["ok"] = False
    except Exception as exc:
        report["checks"]["db"] = {"ok": False, "error": str(exc)[:200]}
        report["issues"].append(f"DB 连接 / 迁移失败：{str(exc)[:120]}")
        report["ok"] = False

    # Backup dir writable
    backups_dir = Path(settings.db_path).parent / "backups"
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
        probe = backups_dir / ".preflight-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report["checks"]["backup_dir"] = {"ok": True, "path": str(backups_dir)}
    except Exception as exc:
        report["checks"]["backup_dir"] = {"ok": False, "path": str(backups_dir), "error": str(exc)[:200]}
        report["issues"].append(f"备份目录不可写：{backups_dir}")
        report["ok"] = False

    # Token strength
    token_warnings = check_token_strength(
        admin_token=settings.admin_token,
        webhook_secret=settings.webhook_secret,
        host=args.host or settings.web_host,
    )
    report["checks"]["tokens"] = {
        "ok": not token_warnings,
        "host": args.host or settings.web_host,
        "warnings": token_warnings,
    }
    if token_warnings:
        report["warnings"].extend(token_warnings)
        if not args.allow_weak_tokens:
            report["ok"] = False

    # Web API security
    dev_auth_bypass_allowed = getattr(args, "allow_dev_auth_bypass", False)
    public_base_url = (settings.public_base_url or "").strip()
    web_security_ok = (
        (not settings.dev_auth_bypass or dev_auth_bypass_allowed)
        and not (settings.is_production and settings.dev_session_enabled)
        and not (settings.is_production and (not public_base_url or not public_base_url.startswith("https://")))
    )
    report["checks"]["web_security"] = {
        "ok": web_security_ok,
        "environment": settings.environment,
        "public_base_url": public_base_url,
        "dev_auth_bypass": settings.dev_auth_bypass,
        "dev_session_enabled": settings.dev_session_enabled,
        "allow_dev_auth_bypass": dev_auth_bypass_allowed,
        "allowed_origins": settings.web_allowed_origins,
        "session_cookie_secure": settings.session_cookie_secure,
        "session_cookie_samesite": settings.session_cookie_samesite,
        "audit_retention_days": settings.audit_retention_days,
        "audit_export_max_rows": settings.audit_export_max_rows,
    }
    if settings.dev_auth_bypass:
        message = "CHABO_DEV_AUTH_BYPASS 已开启；生产部署必须关闭开发期免登录"
        if dev_auth_bypass_allowed:
            report["warnings"].append(f"{message}（local 验收已显式放行）")
        else:
            report["issues"].append(message)
            report["ok"] = False
    if settings.is_production and settings.dev_session_enabled:
        report["issues"].append("CHABO_DEV_SESSION_ENABLED 已开启；生产部署必须关闭开发授权表单")
        report["ok"] = False
    if settings.is_production and (not public_base_url or not public_base_url.startswith("https://")):
        report["issues"].append("CHABO_PUBLIC_BASE_URL 生产环境必须配置为 HTTPS 地址，用于 Bot 生成网页端登录链接")
        report["ok"] = False
    if settings.is_production and settings.audit_retention_days < 365:
        report["issues"].append("CHABO_AUDIT_RETENTION_DAYS 生产环境建议至少保留 365 天")
        report["ok"] = False
    if settings.audit_export_max_rows <= 0:
        report["issues"].append("CHABO_AUDIT_EXPORT_MAX_ROWS 必须大于 0")
        report["ok"] = False

    # Bot config sanity
    bot_token_set = bool(settings.bot_token)
    bot_username_set = bool(settings.bot_username)
    bot_ok = (bot_token_set and bot_username_set) or (not bot_token_set and not bot_username_set)
    report["checks"]["bot"] = {
        "ok": bot_ok,
        "bot_token_set": bot_token_set,
        "bot_username_set": bot_username_set,
    }
    if not bot_ok:
        report["warnings"].append("CHABO_BOT_TOKEN 与 CHABO_BOT_USERNAME 必须同时配置或同时不配置")

    # Operational counters
    try:
        app = create_app(settings)
        with app.db.transaction() as conn:
            counters = {
                "pending_review_orders": conn.execute(
                    "SELECT COUNT(*) AS n FROM ad_orders WHERE status = 'pending_review'"
                ).fetchone()["n"],
                "running_orders": conn.execute(
                    "SELECT COUNT(*) AS n FROM ad_orders WHERE status = 'running'"
                ).fetchone()["n"],
                "scheduled_due": conn.execute(
                    "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'scheduled' "
                    "AND scheduled_at <= datetime('now')"
                ).fetchone()["n"],
                "open_disputes": conn.execute(
                    "SELECT COUNT(*) AS n FROM disputes WHERE status = 'open'"
                ).fetchone()["n"],
                "failed_recent": conn.execute(
                    "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'failed' "
                    "AND updated_at >= datetime('now', '-1 day')"
                ).fetchone()["n"],
                "pending_topups": conn.execute(
                    "SELECT COUNT(*) AS n FROM topup_requests WHERE status = 'pending'"
                ).fetchone()["n"],
            }
        report["checks"]["ops"] = counters
        if counters["scheduled_due"] > 0:
            report["warnings"].append(
                f"{counters['scheduled_due']} 个到期投放未发送，启动后请尽快跑 dispatch-due"
            )
    except Exception as exc:
        report["checks"]["ops"] = {"error": str(exc)[:200]}
        report["warnings"].append(f"运营计数读取失败：{str(exc)[:120]}")

    print_json(report)
    if not report["ok"]:
        sys.exit(2)


def _verify_command(
    steps: list[dict[str, Any]],
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
) -> None:
    step: dict[str, Any] = {
        "name": name,
        "type": "command",
        "command": command,
        "cwd": str(cwd),
    }
    if dry_run:
        step["status"] = "planned"
        steps.append(step)
        return
    started = time.monotonic()
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = proc.stdout or ""
    step.update(
        {
            "status": "passed" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "duration_seconds": round(time.monotonic() - started, 2),
            "output_tail": output.splitlines()[-30:],
        }
    )
    steps.append(step)


def _verify_action(
    steps: list[dict[str, Any]],
    name: str,
    target: dict[str, Any],
    action: Any,
    *,
    dry_run: bool = False,
) -> None:
    step: dict[str, Any] = {"name": name, "type": "action", **target}
    if dry_run:
        step["status"] = "planned"
        steps.append(step)
        return
    started = time.monotonic()
    try:
        result = action()
    except Exception as exc:
        step.update(
            {
                "status": "failed",
                "duration_seconds": round(time.monotonic() - started, 2),
                "error": str(exc)[:300],
            }
        )
    else:
        step.update(
            {
                "status": "passed",
                "duration_seconds": round(time.monotonic() - started, 2),
                "result": result,
            }
        )
    steps.append(step)


def _read_health(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"health returned not ok: {payload}")
    return {"ok": payload.get("ok"), "ops": payload.get("ops", {})}


def cmd_backup_db(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    target = args.target
    if not target:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backups_dir = Path(settings.db_path).parent / "backups"
        target = str(backups_dir / f"chabo-{timestamp}.sqlite3")
    written_to = Database(settings.db_path).backup_to(target)
    print_json({"source": settings.db_path, "backup": written_to})


def cmd_topup(args: argparse.Namespace) -> None:
    app = create_app()
    account = app.ledger.manual_topup(
        args.telegram_user_id,
        money_to_cents(args.amount),
        display_name=args.display_name,
        memo=args.memo,
    )
    print_json(_account_view(account))


def cmd_show_account(args: argparse.Namespace) -> None:
    app = create_app()
    with app.db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE telegram_user_id = ? OR id = ?",
            (args.account, args.account),
        ).fetchone()
        if not row:
            raise NotFound(f"account not found: {args.account}")
        print_json(_account_view(dict(row)))


def cmd_bind_channel(args: argparse.Namespace) -> None:
    app = create_app()
    channel = app.channels.bind_channel(
        args.telegram_chat_id,
        args.title,
        args.username,
        args.owner_telegram_user_id,
        args.owner_display_name,
    )
    print_json({"channel": channel, "start_url": app.channels.start_url(channel)})


def cmd_set_rate(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    updated = app.channels.update_rate(channel["id"], args.slot_type, money_to_cents(args.amount))
    print_json(updated)


def cmd_set_format_policy(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    policy = app.channels.set_format_policy(
        channel["id"],
        args.format_type,
        enabled=args.enabled,
        owner_price_band=args.owner_price_band,
        platform_promo_enabled=args.platform_promo_enabled,
        custom_multiplier_bps=args.custom_multiplier_bps,
    )
    print_json(policy)


def cmd_assess_channel(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    assessment = app.pricing.assess_channel(
        channel_id=channel["id"],
        category=args.category,
        median_24h_views=args.median_24h_views,
        subscribers=args.subscribers,
        light_clicks_30d=args.light_clicks_30d,
        light_unique_clickers_30d=args.light_unique_clickers_30d,
        repeat_purchase_count=args.repeat_purchase_count,
        dispute_count=args.dispute_count,
        risk_level=args.risk_level,
    )
    print_json(assessment)


def cmd_quote_channel(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    quote = app.pricing.quote_channel(channel["id"], args.slot_type, args.owner_price_band)
    print_json(quote)


def cmd_apply_pricing(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    print_json(app.pricing.apply_quotes_to_rate_cards(channel["id"]))


def cmd_make_offer(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    offer = app.price_offers.create_offer(
        advertiser_telegram_user_id=args.advertiser_telegram_user_id,
        channel_id=channel["id"],
        slot_type=args.slot_type,
        offered_price_cents=money_to_cents(args.amount),
        creative_text=args.text,
        target_url=args.target_url,
        budget_cents=money_to_cents(args.budget) if args.budget else None,
        button_text=args.button_text,
        category=args.category,
        scheduled_at=datetime.fromisoformat(args.scheduled_at) if args.scheduled_at else None,
        end_at=datetime.fromisoformat(args.end_at) if args.end_at else None,
        frequency_per_day=args.frequency_per_day,
        message=args.message,
    )
    print_json(offer)


def cmd_quote_subscription(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.subscriptions.quote(args.subscribers))


def cmd_activate_subscription(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    subscription = app.subscriptions.activate(
        channel_id=channel["id"],
        subscriber_count=args.subscribers,
        months=args.months,
    )
    print_json(subscription)


def cmd_purchase_subscription(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    subscription = app.subscriptions.purchase(
        channel_id=channel["id"],
        subscriber_count=args.subscribers,
        months=args.months,
    )
    print_json(subscription)


def cmd_show_subscription(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    print_json(app.subscriptions.get_active(channel["id"]) or {"active": False})


def cmd_quote_advertiser_plan(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.advertiser_subscriptions.quote(args.plan))


def cmd_purchase_advertiser_plan(args: argparse.Namespace) -> None:
    app = create_app()
    subscription = app.advertiser_subscriptions.purchase(
        advertiser_telegram_user_id=args.advertiser_telegram_user_id,
        plan=args.plan,
        months=args.months,
    )
    print_json(subscription)


def cmd_show_advertiser_plan(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.advertiser_subscriptions.status(args.advertiser_telegram_user_id))


def _send_stars_invoice(app: Any, invoice_response: dict[str, Any], chat_id: str | int | None = None) -> dict[str, Any]:
    invoice = invoice_response["invoice"]
    intent = invoice_response["intent"]
    message_id = app.gateway.send_invoice(
        chat_id=chat_id or intent["telegram_user_id"],
        title=invoice["title"],
        description=invoice["description"],
        payload=invoice["payload"],
        currency=invoice["currency"],
        prices=invoice["prices"],
    )
    return {**invoice_response, "message_id": message_id}


def cmd_send_stars_topup_invoice(args: argparse.Namespace) -> None:
    app = create_app()
    invoice_response = app.stars_payments.create_balance_topup_invoice(
        telegram_user_id=args.telegram_user_id,
        stars_amount=args.stars,
        display_name=args.display_name,
    )
    print_json(_send_stars_invoice(app, invoice_response, args.chat_id))


def cmd_send_publisher_subscription_invoice(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    invoice_response = app.stars_payments.create_publisher_subscription_invoice(
        channel_id=channel["id"],
        subscriber_count=args.subscribers,
        months=args.months,
    )
    print_json(_send_stars_invoice(app, invoice_response, args.chat_id))


def cmd_send_advertiser_plan_invoice(args: argparse.Namespace) -> None:
    app = create_app()
    invoice_response = app.stars_payments.create_advertiser_subscription_invoice(
        advertiser_telegram_user_id=args.advertiser_telegram_user_id,
        plan=args.plan,
        months=args.months,
    )
    print_json(_send_stars_invoice(app, invoice_response, args.chat_id))


def cmd_show_stars_payment_intent(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.stars_payments.get_intent(args.intent))


def cmd_create_probe(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    probe = app.light_probes.create_probe(
        channel_id=channel["id"],
        short_text=args.short_text,
        detail_text=args.detail_text,
        target_url=args.target_url,
        button_text=args.button_text,
        start_at=datetime.fromisoformat(args.start_at) if args.start_at else None,
        end_at=datetime.fromisoformat(args.end_at) if args.end_at else None,
    )
    print_json({"probe": probe, "start_url": app.light_probes.start_url(probe)})


def cmd_pause_probe(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.light_probes.pause_probe(args.probe_id))


def cmd_probe_stats(args: argparse.Namespace) -> None:
    app = create_app()
    channel_id = None
    if args.channel:
        channel = _find_channel(app, args.channel)
        channel_id = channel["id"]
    print_json(app.light_probes.stats(channel_id=channel_id, probe_id=args.probe_id))


def cmd_discover_channels(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(
        app.advertisers.discover_channels(
            advertiser_telegram_user_id=args.advertiser_telegram_user_id,
            category=args.category,
            min_score=args.min_score,
            max_risk_level=args.max_risk_level,
            max_price_cents=money_to_cents(args.max_price) if args.max_price else None,
            slot_type=args.slot_type,
            limit=args.limit,
        )
    )


def cmd_save_channel(args: argparse.Namespace) -> None:
    app = create_app()
    channel = _find_channel(app, args.channel)
    print_json(
        app.advertisers.save_channel(
            advertiser_telegram_user_id=args.advertiser_telegram_user_id,
            channel_id=channel["id"],
            note=args.note,
        )
    )


def cmd_list_saved_channels(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.advertisers.list_saved_channels(args.advertiser_telegram_user_id))


def cmd_create_alert_rule(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(
        app.advertisers.create_alert_rule(
            advertiser_telegram_user_id=args.advertiser_telegram_user_id,
            category=args.category,
            min_score=args.min_score,
            max_risk_level=args.max_risk_level,
            max_price_cents=money_to_cents(args.max_price) if args.max_price else None,
            slot_type=args.slot_type,
        )
    )


def cmd_scan_alerts(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.advertisers.scan_alerts(args.advertiser_telegram_user_id))


def cmd_list_alerts(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.advertisers.list_alert_events(args.advertiser_telegram_user_id, status=args.status))


def cmd_advertiser_report(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.advertisers.report(args.advertiser_telegram_user_id))


def cmd_batch_orders(args: argparse.Namespace) -> None:
    app = create_app()
    tokens = [token.strip() for token in args.channel_tokens.split(",") if token.strip()]
    kwargs: dict[str, Any] = {
        "advertiser_telegram_user_id": args.advertiser_telegram_user_id,
        "channel_tokens": tokens,
        "slot_type": args.slot_type,
        "budget_cents": money_to_cents(args.budget),
        "button_text": args.button_text,
        "category": args.category,
    }
    if args.material_id:
        kwargs["material_id"] = args.material_id
    else:
        kwargs["text"] = args.text
        kwargs["target_url"] = args.target_url
    print_json(app.advertisers.create_batch_orders(**kwargs))


def cmd_respond_offer(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.price_offers.respond_offer(args.offer_id, accepted=args.accept))


def cmd_create_order(args: argparse.Namespace) -> None:
    app = create_app()
    order = app.orders.create_order(
        advertiser_telegram_user_id=args.advertiser_telegram_user_id,
        channel_token=args.channel_token,
        slot_type=args.slot_type,
        text=args.text,
        target_url=args.target_url,
        budget_cents=money_to_cents(args.budget),
        button_text=args.button_text,
        category=args.category,
        light_short_text=args.light_short_text,
        material_id=args.material_id,
        scheduled_at=datetime.fromisoformat(args.scheduled_at) if args.scheduled_at else None,
        end_at=datetime.fromisoformat(args.end_at) if args.end_at else None,
        frequency_per_day=args.frequency_per_day,
        campaign_name=args.campaign_name,
    )
    print_json(order)


def cmd_create_material(args: argparse.Namespace) -> None:
    app = create_app()
    material = app.materials.create_material(
        advertiser_telegram_user_id=args.advertiser_telegram_user_id,
        format_type=args.format_type,
        text=args.text,
        target_url=args.target_url,
        button_text=args.button_text,
        category=args.category,
        light_short_text=args.light_short_text,
        display_name=args.display_name,
    )
    print_json(material)


def cmd_list_materials(args: argparse.Namespace) -> None:
    app = create_app()
    items = app.materials.list_materials(
        advertiser_telegram_user_id=args.advertiser_telegram_user_id,
        format_type=args.format_type,
        include_archived=args.include_archived,
        limit=args.limit,
    )
    print_json(items)


def cmd_show_material(args: argparse.Namespace) -> None:
    app = create_app()
    material = app.materials.get_material(
        args.material_id,
        advertiser_telegram_user_id=args.advertiser_telegram_user_id,
    )
    print_json(material)


def cmd_archive_material(args: argparse.Namespace) -> None:
    app = create_app()
    material = app.materials.archive_material(
        args.material_id,
        advertiser_telegram_user_id=args.advertiser_telegram_user_id,
    )
    print_json(material)


def cmd_request_topup(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(
        app.topup_approvals.request_topup(
            recipient_telegram_user_id=args.recipient_telegram_user_id,
            amount_cents=money_to_cents(args.amount),
            reason=args.reason,
            requester_telegram_user_id=args.requester_telegram_user_id,
            evidence_url=args.evidence_url,
            request_note=args.note,
        )
    )


def cmd_approve_topup(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(
        app.topup_approvals.approve_topup(
            request_id=args.request_id,
            approver_telegram_user_id=args.approver_telegram_user_id,
            approval_note=args.note,
        )
    )


def cmd_reject_topup(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(
        app.topup_approvals.reject_topup(
            request_id=args.request_id,
            approver_telegram_user_id=args.approver_telegram_user_id,
            approval_note=args.note,
        )
    )


def cmd_list_topup_requests(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(
        app.topup_approvals.list_requests(status=args.status, limit=args.limit)
    )


def cmd_list_tool_calls(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(
        app.tool_call_logs.list_calls(
            actor_telegram_user_id=args.actor_telegram_user_id,
            tool_name=args.tool_name,
            result_status=args.result_status,
            limit=args.limit,
        )
    )


def cmd_log_tool_call(args: argparse.Namespace) -> None:
    app = create_app()
    arguments = json.loads(args.arguments) if args.arguments else {}
    print_json(
        app.tool_call_logs.log_call(
            tool_name=args.tool_name,
            actor_telegram_user_id=args.actor_telegram_user_id,
            actor_kind=args.actor_kind,
            session_id=args.session_id,
            arguments=arguments,
            result_status=args.result_status,
            result_summary=args.result_summary,
            error_type=args.error_type,
        )
    )


def cmd_approve_order(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.orders.approve_order(args.order_id))


def cmd_reject_order(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.orders.reject_order(args.order_id, args.reason))


def cmd_refund_delivery(args: argparse.Namespace) -> None:
    app = create_app()
    if args.amount:
        print_json(app.orders.refund_delivery_partial(args.delivery_id, money_to_cents(args.amount), args.reason))
    else:
        print_json(app.orders.refund_delivery(args.delivery_id, args.reason))


def cmd_dispatch_due(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.fulfillment.dispatch_due(args.limit))


def cmd_confirm_earnings(args: argparse.Namespace) -> None:
    app = create_app()
    count = app.fulfillment.confirm_due_earnings(args.observation_hours)
    print_json({"confirmed_deliveries": count})


def cmd_list_orders(args: argparse.Namespace) -> None:
    app = create_app()
    with app.db.transaction() as conn:
        rows = conn.execute(
            """
            SELECT o.id, o.status, o.budget_cents, o.reserved_cents, o.spent_cents,
                   o.scheduled_at, c.title AS channel_title, cr.text AS creative_text
            FROM ad_orders o
            JOIN channels c ON c.id = o.channel_id
            JOIN creatives cr ON cr.id = o.creative_id
            ORDER BY o.created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print_json([dict(row) for row in rows])


def cmd_open_dispute(args: argparse.Namespace) -> None:
    app = create_app()
    dispute = app.disputes.open_dispute(
        opened_by_telegram_user_id=args.opened_by_telegram_user_id,
        delivery_id=args.delivery_id,
        reason=args.reason,
    )
    print_json(dispute)


def cmd_list_disputes(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.disputes.list_disputes(status=args.status, limit=args.limit))


def cmd_resolve_dispute(args: argparse.Namespace) -> None:
    app = create_app()
    print_json(app.disputes.resolve_dispute(dispute_id=args.dispute_id, resolution=args.resolution))


def cmd_handle_update(args: argparse.Namespace) -> None:
    app = create_app()
    update = json.loads(Path(args.file).read_text(encoding="utf-8")) if args.file else json.load(sys.stdin)
    print_json(app.update_handler.handle(update))


def cmd_run_polling(args: argparse.Namespace) -> None:
    from .polling import PollingRunner

    settings = Settings.from_env()
    PollingRunner(settings).run(
        timeout=args.timeout,
        limit=args.limit,
        once=args.once,
        drop_pending_updates=args.drop_pending_updates,
    )


def cmd_run_web(args: argparse.Namespace) -> None:
    from .web import run_server

    settings = Settings.from_env()
    run_server(
        settings=settings,
        host=args.host,
        port=args.port,
        admin_token=args.admin_token,
        webhook_secret=args.webhook_secret,
    )


def cmd_run_api(args: argparse.Namespace) -> None:
    from .webapi.main import run_api

    settings = Settings.from_env()
    run_api(settings=settings, host=args.host, port=args.port)


def cmd_set_webhook(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    if not settings.bot_token:
        raise RuntimeError("CHABO_BOT_TOKEN is required")
    from .telegram import BotApiClient

    secret = args.secret or settings.webhook_secret
    result = BotApiClient(settings.bot_token, settings.telegram_http_backend).set_webhook(
        url=args.url,
        secret_token=secret,
        drop_pending_updates=args.drop_pending_updates,
    )
    print_json({"ok": result, "url": args.url, "secret_configured": bool(secret)})


def _find_channel(app: Any, identifier: str) -> dict[str, Any]:
    with app.db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM channels WHERE id = ? OR ref_token = ? OR telegram_chat_id = ? OR username = ?",
            (identifier, identifier, identifier, identifier),
        ).fetchone()
        if not row:
            raise NotFound(f"channel not found: {identifier}")
        return dict(row)


def _account_view(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": account["id"],
        "telegram_user_id": account["telegram_user_id"],
        "role": account["role"],
        "display_name": account["display_name"],
        "available": cents_to_money(account["available_balance_cents"]),
        "reserved": cents_to_money(account["reserved_balance_cents"]),
        "spent": cents_to_money(account["spent_balance_cents"]),
        "pending_earnings": cents_to_money(account["pending_earnings_cents"]),
        "confirmed_earnings": cents_to_money(account["confirmed_earnings_cents"]),
        "releasable_earnings": cents_to_money(account["releasable_earnings_cents"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chabo", description="插播 Telegram 广告插播 MVP 管理工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backup-db", help="把 SQLite 数据库备份到文件；不传 --target 时自动写到 <db_dir>/backups/chabo-YYYYMMDD-HHMMSS.sqlite3")
    p.add_argument("--target", help="备份输出路径")
    p.set_defaults(func=cmd_backup_db)

    p = sub.add_parser("preflight", help="生产部署前自检：DB / schema / 备份目录 / token 强度 / Bot 配置 / 运营计数；critical 问题非零退出")
    p.add_argument("--host", help="覆盖 web_host 用于 token 强度判断（loopback 可放过缺 token）")
    p.add_argument("--allow-weak-tokens", action="store_true", help="只在告警里报 token 弱，不影响退出码（仅限 staging 调试用）")
    p.add_argument("--allow-dev-auth-bypass", action="store_true", help="仅 local/staging 验收使用：允许开发期免登录开关")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("init-db", help="初始化 SQLite 数据库")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("verify-web", help="一键验收 React 网页端：Python 测试、前端构建、preflight、health，可选 H5 冒烟")
    p.add_argument("--profile", choices=["local", "production"], default="local")
    p.add_argument("--host", default="127.0.0.1", help="传给 preflight 的 host；production 建议填正式域名")
    p.add_argument("--health-url", help="默认 http://127.0.0.1:8081/api/health")
    p.add_argument("--skip-tests", action="store_true")
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--skip-preflight", action="store_true")
    p.add_argument("--skip-health", action="store_true")
    p.add_argument("--audit-chain", action="store_true", help="额外校验 audit_logs hash 链完整性")
    p.add_argument("--strict-audit-chain", action="store_true", help="审计链校验时把旧的未签名审计记录也视为失败")
    p.add_argument("--allow-weak-tokens", action="store_true", help="production profile 下也放行弱 token；仅 staging 调试用")
    p.add_argument("--allow-dev-auth-bypass", action="store_true", help="production profile 下也放行开发免登录；仅 staging 调试用")
    p.add_argument("--seed-demo", action="store_true", help="local profile 下先补齐演示数据")
    p.add_argument("--seed-telegram-user-id", default="10001")
    p.add_argument("--seed-display-name", default="H5 冒烟用户")
    p.add_argument("--seed-pending-orders", type=int, default=3)
    p.add_argument("--h5-smoke", action="store_true", help="额外运行 web/scripts/h5-smoke.mjs")
    p.add_argument("--h5-smoke-auth-bypass", action="store_true", help="H5 冒烟使用开发免登录")
    p.add_argument("--h5-smoke-base-url")
    p.add_argument("--h5-smoke-api-url")
    p.add_argument("--dry-run", action="store_true", help="只输出将执行的步骤，不真正运行")
    p.set_defaults(func=cmd_verify_web)

    p = sub.add_parser("verify-audit-chain", help="校验 audit_logs 的链式 hash 完整性；发现篡改或断链时非零退出")
    p.add_argument("--created-from", help="只校验该时间之后的审计记录，例如 2026-05-01 00:00:00")
    p.add_argument("--created-to", help="只校验该时间之前的审计记录，例如 2026-05-02 23:59:59")
    p.add_argument("--strict", action="store_true", help="把旧的未签名审计记录也视为失败")
    p.set_defaults(func=cmd_verify_audit_chain)

    p = sub.add_parser("seed-web-demo", help="生成 React 网页端本地演示数据：三端权限、频道、素材、待审订单和待审入账")
    p.add_argument("--telegram-user-id", default="10001", help="默认与 CHABO_DEV_AUTH_TELEGRAM_USER_ID 对齐")
    p.add_argument("--display-name", default="H5 冒烟用户")
    p.add_argument("--pending-orders", type=int, default=3, help="至少保留多少个演示待审订单")
    p.set_defaults(func=cmd_seed_web_demo)

    p = sub.add_parser("topup", help="人工入账广告主插播余额")
    p.add_argument("--telegram-user-id", required=True)
    p.add_argument("--amount", required=True, help="金额，例如 100 或 100.50")
    p.add_argument("--display-name")
    p.add_argument("--memo", default="人工入账")
    p.set_defaults(func=cmd_topup)

    p = sub.add_parser("show-account", help="查看账户余额")
    p.add_argument("account", help="account_id 或 telegram_user_id")
    p.set_defaults(func=cmd_show_account)

    p = sub.add_parser("bind-channel", help="绑定频道并生成插播入口")
    p.add_argument("--telegram-chat-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--username")
    p.add_argument("--owner-telegram-user-id", required=True)
    p.add_argument("--owner-display-name")
    p.set_defaults(func=cmd_bind_channel)

    p = sub.add_parser("set-rate", help="设置频道广告位刊例价")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--slot-type", required=True, choices=["button_tail", "standard", "standard_card", "strong_post", "pin24h", "loop_daily"])
    p.add_argument("--amount", required=True)
    p.set_defaults(func=cmd_set_rate)

    p = sub.add_parser("set-format-policy", help="设置频道主接受的插播广告形态")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--format-type", required=True, choices=["button_tail", "standard", "standard_card", "strong_post", "pin24h", "loop_daily"])
    p.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--owner-price-band", default="medium", choices=["low", "medium", "high", "custom"])
    p.add_argument("--platform-promo-enabled", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--custom-multiplier-bps", type=int)
    p.set_defaults(func=cmd_set_format_policy)

    p = sub.add_parser("assess-channel", help="生成频道插播定价评估")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--category", default="general")
    p.add_argument("--median-24h-views", type=int, default=0)
    p.add_argument("--subscribers", type=int, default=0)
    p.add_argument("--light-clicks-30d", type=int, default=0)
    p.add_argument("--light-unique-clickers-30d", type=int, default=0)
    p.add_argument("--repeat-purchase-count", type=int, default=0)
    p.add_argument("--dispute-count", type=int, default=0)
    p.add_argument("--risk-level", default="normal", choices=["normal", "watch", "high", "blocked"])
    p.set_defaults(func=cmd_assess_channel)

    p = sub.add_parser("quote-channel", help="按最新评估给频道广告形态报价")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--slot-type", required=True, choices=["button_tail", "standard", "standard_card", "strong_post", "pin24h", "loop_daily"])
    p.add_argument("--owner-price-band", choices=["low", "medium", "high", "custom"])
    p.set_defaults(func=cmd_quote_channel)

    p = sub.add_parser("apply-pricing", help="把最新报价写入频道刊例价")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.set_defaults(func=cmd_apply_pricing)

    p = sub.add_parser("make-offer", help="广告主对频道发起砍价报价")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--slot-type", required=True, choices=["button_tail", "standard", "standard_card", "strong_post", "pin24h", "loop_daily"])
    p.add_argument("--amount", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--target-url", required=True)
    p.add_argument("--budget", help="不填则默认等于报价金额")
    p.add_argument("--button-text", default="查看详情")
    p.add_argument("--category", default="general")
    p.add_argument("--scheduled-at", help="ISO 时间，默认立即")
    p.add_argument("--end-at", help="循环插播可用")
    p.add_argument("--frequency-per-day", type=int, default=1)
    p.add_argument("--message")
    p.set_defaults(func=cmd_make_offer)

    p = sub.add_parser("quote-subscription", help="按订阅人数计算频道高级订阅月费")
    p.add_argument("--subscribers", type=int, required=True)
    p.set_defaults(func=cmd_quote_subscription)

    p = sub.add_parser("activate-subscription", help="开通频道高级订阅")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--subscribers", type=int, required=True)
    p.add_argument("--months", type=int, default=1)
    p.set_defaults(func=cmd_activate_subscription)

    p = sub.add_parser("purchase-subscription", help="从频道主余额扣款购买频道高级订阅")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--subscribers", type=int, required=True)
    p.add_argument("--months", type=int, default=1)
    p.set_defaults(func=cmd_purchase_subscription)

    p = sub.add_parser("show-subscription", help="查看频道当前高级订阅")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.set_defaults(func=cmd_show_subscription)

    p = sub.add_parser("quote-advertiser-plan", help="查看广告主高级服务套餐")
    p.add_argument("--plan", required=True, choices=["pro", "enterprise"])
    p.set_defaults(func=cmd_quote_advertiser_plan)

    p = sub.add_parser("purchase-advertiser-plan", help="从广告主余额扣款购买高级服务")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--plan", required=True, choices=["pro", "enterprise"])
    p.add_argument("--months", type=int, default=1)
    p.set_defaults(func=cmd_purchase_advertiser_plan)

    p = sub.add_parser("show-advertiser-plan", help="查看广告主高级服务状态")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.set_defaults(func=cmd_show_advertiser_plan)

    p = sub.add_parser("send-stars-topup-invoice", help="发送广告主 Stars 余额充值发票")
    p.add_argument("--telegram-user-id", required=True)
    p.add_argument("--stars", type=int, required=True, help="Telegram Stars 数量")
    p.add_argument("--display-name")
    p.add_argument("--chat-id", help="默认发给 telegram-user-id")
    p.set_defaults(func=cmd_send_stars_topup_invoice)

    p = sub.add_parser("send-publisher-subscription-invoice", help="发送频道主高级订阅 Stars 发票")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--subscribers", type=int, required=True)
    p.add_argument("--months", type=int, default=1)
    p.add_argument("--chat-id", help="默认发给频道主账号")
    p.set_defaults(func=cmd_send_publisher_subscription_invoice)

    p = sub.add_parser("send-advertiser-plan-invoice", help="发送广告主高级服务 Stars 发票")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--plan", required=True, choices=["pro", "enterprise"])
    p.add_argument("--months", type=int, default=1)
    p.add_argument("--chat-id", help="默认发给广告主")
    p.set_defaults(func=cmd_send_advertiser_plan_invoice)

    p = sub.add_parser("show-stars-payment-intent", help="查看 Stars 支付意图")
    p.add_argument("intent", help="intent_id 或 invoice payload")
    p.set_defaults(func=cmd_show_stars_payment_intent)

    p = sub.add_parser("create-probe", help="创建频道探针按钮")
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--short-text", required=True, help="短文案，用于运营识别")
    p.add_argument("--detail-text", required=True, help="用户点击后在 Bot 内看到的详情")
    p.add_argument("--target-url", required=True)
    p.add_argument("--button-text", default="了解详情")
    p.add_argument("--start-at", help="ISO 时间，默认立即")
    p.add_argument("--end-at", help="ISO 时间")
    p.set_defaults(func=cmd_create_probe)

    p = sub.add_parser("pause-probe", help="暂停频道探针按钮")
    p.add_argument("--probe-id", required=True)
    p.set_defaults(func=cmd_pause_probe)

    p = sub.add_parser("probe-stats", help="查看频道探针按钮点击统计")
    p.add_argument("--channel", help="channel_id/ref_token/chat_id/username")
    p.add_argument("--probe-id")
    p.set_defaults(func=cmd_probe_stats)

    p = sub.add_parser("discover-channels", help="广告主发现优质频道")
    p.add_argument("--advertiser-telegram-user-id", help="传入后按广告主订阅套餐放开发现数量")
    p.add_argument("--category")
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("--max-risk-level", default="watch", choices=["normal", "watch", "high", "blocked"])
    p.add_argument("--max-price")
    p.add_argument("--slot-type", default="standard_card", choices=["button_tail", "standard", "standard_card", "strong_post", "pin24h", "loop_daily"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_discover_channels)

    p = sub.add_parser("save-channel", help="广告主收藏频道")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--channel", required=True, help="channel_id/ref_token/chat_id/username")
    p.add_argument("--note")
    p.set_defaults(func=cmd_save_channel)

    p = sub.add_parser("list-saved-channels", help="查看广告主收藏频道")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.set_defaults(func=cmd_list_saved_channels)

    p = sub.add_parser("create-alert-rule", help="创建新频道/优质频道提醒规则")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--category")
    p.add_argument("--min-score", type=int, default=70)
    p.add_argument("--max-risk-level", default="normal", choices=["normal", "watch", "high", "blocked"])
    p.add_argument("--max-price")
    p.add_argument("--slot-type", default="standard_card", choices=["button_tail", "standard", "standard_card", "strong_post", "pin24h", "loop_daily"])
    p.set_defaults(func=cmd_create_alert_rule)

    p = sub.add_parser("scan-alerts", help="扫描并生成广告主频道提醒事件")
    p.add_argument("--advertiser-telegram-user-id")
    p.set_defaults(func=cmd_scan_alerts)

    p = sub.add_parser("list-alerts", help="查看广告主频道提醒事件")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--status", default="new")
    p.set_defaults(func=cmd_list_alerts)

    p = sub.add_parser("advertiser-report", help="查看广告主投放报表")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.set_defaults(func=cmd_advertiser_report)

    p = sub.add_parser("batch-orders", help="批量创建插播订单")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--channel-tokens", required=True, help="多个 ref_token，用逗号分隔")
    p.add_argument("--slot-type", required=True, choices=["button_tail", "standard", "standard_card", "strong_post", "pin24h", "loop_daily"])
    p.add_argument("--material-id", help="复用广告库已有素材；与 --text/--target-url 二选一")
    p.add_argument("--text", help="提供 --material-id 时无需传入")
    p.add_argument("--target-url", help="提供 --material-id 时无需传入")
    p.add_argument("--budget", required=True, help="每个频道的预算")
    p.add_argument("--button-text", default="查看详情")
    p.add_argument("--category", default="general")
    p.set_defaults(func=cmd_batch_orders)

    p = sub.add_parser("respond-offer", help="频道主接受或拒绝砍价报价")
    p.add_argument("--offer-id", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--accept", action="store_true")
    group.add_argument("--reject", action="store_true")
    p.set_defaults(func=cmd_respond_offer)

    p = sub.add_parser("create-order", help="创建插播订单并冻结预算")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--channel-token", required=True)
    p.add_argument("--slot-type", required=True, choices=["button_tail", "standard", "standard_card", "strong_post", "pin24h", "loop_daily"])
    p.add_argument("--material-id", help="复用广告库已有素材；与 --text/--target-url 二选一")
    p.add_argument("--text", help="标准/定制插播文案；提供 --material-id 时无需传入")
    p.add_argument("--target-url", help="提供 --material-id 时无需传入")
    p.add_argument("--budget", required=True)
    p.add_argument("--button-text", default="查看详情")
    p.add_argument("--category", default="general")
    p.add_argument("--light-short-text", help=argparse.SUPPRESS)
    p.add_argument("--scheduled-at", help="ISO 时间，默认立即")
    p.add_argument("--end-at", help="ISO 时间，循环插播可用")
    p.add_argument("--frequency-per-day", type=int, default=1)
    p.add_argument("--campaign-name", default="插播广告")
    p.set_defaults(func=cmd_create_order)

    p = sub.add_parser("create-material", help="把广告素材保存到广告库")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument(
        "--format-type",
        required=True,
        choices=["button_tail", "standard_card", "strong_post"],
        help="按钮插播=button_tail / 标准插播=standard_card / 定制插播=strong_post",
    )
    p.add_argument("--text", required=True, help="完整广告文案")
    p.add_argument("--target-url", required=True)
    p.add_argument("--button-text", default="查看详情")
    p.add_argument("--category", default="general")
    p.add_argument("--light-short-text", help=argparse.SUPPRESS)
    p.add_argument("--display-name", help="第一次出现广告主时使用的展示名")
    p.set_defaults(func=cmd_create_material)

    p = sub.add_parser("list-materials", help="列出广告主广告库里的素材")
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.add_argument("--format-type", choices=["button_tail", "standard_card", "strong_post"])
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list_materials)

    p = sub.add_parser("show-material", help="查看单条广告库素材")
    p.add_argument("--material-id", required=True)
    p.add_argument(
        "--advertiser-telegram-user-id",
        help="提供后会校验素材归属，避免越权读取",
    )
    p.set_defaults(func=cmd_show_material)

    p = sub.add_parser("archive-material", help="把广告库里的素材归档；归档后不可再用于新订单")
    p.add_argument("--material-id", required=True)
    p.add_argument("--advertiser-telegram-user-id", required=True)
    p.set_defaults(func=cmd_archive_material)

    p = sub.add_parser("request-topup", help="提交人工入账请求（双人复核第一步）")
    p.add_argument("--recipient-telegram-user-id", required=True, help="收款方 Telegram 用户 ID")
    p.add_argument("--amount", required=True, help="美元金额，例如 50.00")
    p.add_argument("--reason", required=True, help="入账原因 / 凭证摘要")
    p.add_argument("--requester-telegram-user-id", required=True, help="申请人 Telegram 用户 ID")
    p.add_argument("--evidence-url", help="链外凭证 URL（截图、对账单等）")
    p.add_argument("--note", help="申请备注")
    p.set_defaults(func=cmd_request_topup)

    p = sub.add_parser("approve-topup", help="审批入账请求；审批人必须与申请人是不同账号")
    p.add_argument("--request-id", required=True)
    p.add_argument("--approver-telegram-user-id", required=True)
    p.add_argument("--note", help="审批备注")
    p.set_defaults(func=cmd_approve_topup)

    p = sub.add_parser("reject-topup", help="拒绝入账请求；不会动账本")
    p.add_argument("--request-id", required=True)
    p.add_argument("--approver-telegram-user-id", required=True)
    p.add_argument("--note", help="拒绝备注")
    p.set_defaults(func=cmd_reject_topup)

    p = sub.add_parser("list-topup-requests", help="查看入账请求列表")
    p.add_argument("--status", choices=["pending", "approved", "rejected"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list_topup_requests)

    p = sub.add_parser("list-tool-calls", help="查看 AI / 运营工具调用审计日志")
    p.add_argument("--actor-telegram-user-id", help="按操作者 Telegram 用户 ID 过滤")
    p.add_argument("--tool-name", help="按工具名过滤，例如 create_material / approve_order")
    p.add_argument("--result-status", choices=["success", "error"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list_tool_calls)

    p = sub.add_parser("log-tool-call", help="手动记录一条工具调用日志（测试或外部 AI 调用回写）")
    p.add_argument("--tool-name", required=True)
    p.add_argument("--actor-telegram-user-id")
    p.add_argument("--actor-kind", choices=["human", "ai", "admin", "system"], default="human")
    p.add_argument("--session-id")
    p.add_argument("--arguments", help="JSON 字符串，调用参数摘要")
    p.add_argument("--result-status", choices=["success", "error"], default="success")
    p.add_argument("--result-summary")
    p.add_argument("--error-type")
    p.set_defaults(func=cmd_log_tool_call)

    p = sub.add_parser("approve-order", help="审核通过订单并创建首个投放任务")
    p.add_argument("--order-id", required=True)
    p.set_defaults(func=cmd_approve_order)

    p = sub.add_parser("reject-order", help="审核拒绝订单并释放冻结预算")
    p.add_argument("--order-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_reject_order)

    p = sub.add_parser("refund-delivery", help="对已扣费投放执行退款；不传 amount 时全额退款")
    p.add_argument("--delivery-id", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--amount", help="部分退款金额，例如 3.50；不填则退还剩余可退金额")
    p.set_defaults(func=cmd_refund_delivery)

    p = sub.add_parser("dispatch-due", help="调度到期插播任务")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_dispatch_due)

    p = sub.add_parser("confirm-earnings", help="确认观察期到期的频道主收益")
    p.add_argument("--observation-hours", type=int, default=24)
    p.set_defaults(func=cmd_confirm_earnings)

    p = sub.add_parser("list-orders", help="列出订单")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list_orders)

    p = sub.add_parser("open-dispute", help="创建插播争议并保留证据")
    p.add_argument("--opened-by-telegram-user-id", required=True)
    p.add_argument("--delivery-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_open_dispute)

    p = sub.add_parser("list-disputes", help="列出插播争议")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list_disputes)

    p = sub.add_parser("resolve-dispute", help="人工裁决插播争议")
    p.add_argument("--dispute-id", required=True)
    p.add_argument("--resolution", required=True)
    p.set_defaults(func=cmd_resolve_dispute)

    p = sub.add_parser("handle-update", help="从 JSON 文件或 stdin 处理一条 Telegram update")
    p.add_argument("--file")
    p.set_defaults(func=cmd_handle_update)

    p = sub.add_parser("run-polling", help="启动插播 Bot polling 运行器")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--once", action="store_true", help="只拉取并处理一轮 update，适合测试")
    p.add_argument("--drop-pending-updates", action="store_true", help="启动前丢弃 Telegram 侧积压 update")
    p.set_defaults(func=cmd_run_polling)

    p = sub.add_parser("run-web", help="启动插播 HTTP webhook 与运营后台")
    p.add_argument("--host", default=None, help="默认读取 CHABO_WEB_HOST")
    p.add_argument("--port", type=int, default=None, help="默认读取 CHABO_WEB_PORT")
    p.add_argument("--admin-token", help="覆盖 CHABO_ADMIN_TOKEN")
    p.add_argument("--webhook-secret", help="覆盖 CHABO_WEBHOOK_SECRET")
    p.set_defaults(func=cmd_run_web)

    p = sub.add_parser("run-api", help="启动 React 网页端使用的 FastAPI JSON API")
    p.add_argument("--host", default=None, help="默认读取 CHABO_API_HOST")
    p.add_argument("--port", type=int, default=None, help="默认读取 CHABO_API_PORT")
    p.set_defaults(func=cmd_run_api)

    p = sub.add_parser("set-webhook", help="把 Telegram webhook 指向插播 HTTP 服务公网地址")
    p.add_argument("--url", required=True, help="例如 https://example.com/telegram/webhook/<secret>")
    p.add_argument("--secret", help="Telegram secret_token；默认读取 CHABO_WEBHOOK_SECRET")
    p.add_argument("--drop-pending-updates", action="store_true")
    p.set_defaults(func=cmd_set_webhook)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
