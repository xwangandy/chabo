from __future__ import annotations

import hmac
import html
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .app import ChaboApp, create_app
from .config import Settings
from .money import cents_to_money, money_to_cents
from .services import ChaboError


MAX_REQUEST_BYTES = 1 * 1024 * 1024  # 1 MB cap on POST body
logger = logging.getLogger(__name__)


def _safe_eq(provided: str | None, expected: str | None) -> bool:
    """Constant-time string compare. Returns False for missing values."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


class ChaboHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        app: ChaboApp,
        *,
        admin_token: str | None,
        webhook_secret: str | None,
    ) -> None:
        super().__init__(server_address, ChaboRequestHandler)
        self.app = app
        self.admin_token = admin_token
        self.webhook_secret = webhook_secret


def make_server(
    *,
    settings: Settings | None = None,
    app: ChaboApp | None = None,
    host: str | None = None,
    port: int | None = None,
    admin_token: str | None = None,
    webhook_secret: str | None = None,
) -> ChaboHTTPServer:
    settings = settings or Settings.from_env()
    app = app or create_app(settings)
    return ChaboHTTPServer(
        (host or settings.web_host, port if port is not None else settings.web_port),
        app,
        admin_token=admin_token if admin_token is not None else settings.admin_token,
        webhook_secret=webhook_secret if webhook_secret is not None else settings.webhook_secret,
    )


WEAK_TOKEN_PATTERNS = ("test", "demo", "changeme", "default", "secret", "admin", "token")
MIN_PROD_TOKEN_LEN = 16


def check_token_strength(*, admin_token: str | None, webhook_secret: str | None, host: str | None = None) -> list[str]:
    """Return a list of human-readable warnings about weak production tokens.

    Empty list means the configured tokens look OK. The host argument
    enables a "production-only" check that flags missing webhook secrets
    when the server is bound to a non-loopback address.
    """
    warnings: list[str] = []
    is_loopback = host in (None, "", "127.0.0.1", "localhost", "::1")

    def looks_weak(value: str) -> bool:
        lowered = value.lower()
        if len(value) < MIN_PROD_TOKEN_LEN:
            return True
        if any(pattern in lowered for pattern in WEAK_TOKEN_PATTERNS):
            return True
        return False

    if not admin_token:
        if not is_loopback:
            warnings.append("⚠️  CHABO_ADMIN_TOKEN 未配置；非 loopback 地址下 /admin 会拒绝任何请求。")
    elif looks_weak(admin_token):
        warnings.append("⚠️  CHABO_ADMIN_TOKEN 强度不足（少于 16 字符或包含弱关键词），生产部署前请换成强随机值。")

    if not webhook_secret:
        if not is_loopback:
            warnings.append("⚠️  CHABO_WEBHOOK_SECRET 未配置；非 loopback 地址下 Telegram webhook 无法被验证。")
    elif looks_weak(webhook_secret):
        warnings.append("⚠️  CHABO_WEBHOOK_SECRET 强度不足，请使用强随机值并通过 set-webhook 同步到 Telegram。")

    return warnings


def run_server(
    *,
    settings: Settings | None = None,
    host: str | None = None,
    port: int | None = None,
    admin_token: str | None = None,
    webhook_secret: str | None = None,
) -> None:
    server = make_server(
        settings=settings,
        host=host,
        port=port,
        admin_token=admin_token,
        webhook_secret=webhook_secret,
    )
    bound_host, bound_port = server.server_address
    print(f"插播 HTTP 服务已启动：http://{bound_host}:{bound_port}")
    for warning in check_token_strength(
        admin_token=server.admin_token,
        webhook_secret=server.webhook_secret,
        host=str(bound_host),
    ):
        print(warning)
    try:
        server.serve_forever()
    finally:
        server.server_close()


class ChaboRequestHandler(BaseHTTPRequestHandler):
    server: ChaboHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        print(json.dumps({"event": "http_request", "client": self.client_address[0], "message": format % args}, ensure_ascii=False))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(self._build_health_payload())
            return
        if parsed.path == "/admin":
            if not self._require_admin(parsed.query):
                return
            self._send_html(self._render_admin(parsed.query))
            return
        if parsed.path == "/admin/orders":
            if not self._require_admin(parsed.query):
                return
            params = parse_qs(parsed.query)
            status = params.get("status", [None])[0]
            self._send_json({"orders": self._list_orders(status=status)})
            return
        if parsed.path.startswith("/admin/orders/"):
            if not self._require_admin(parsed.query):
                return
            order_id = parsed.path.removeprefix("/admin/orders/").strip("/")
            try:
                detail = self._order_detail(order_id)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if self._wants_json():
                self._send_json({"order": detail})
            else:
                self._send_html(self._render_detail("订单详情", detail, parsed.query))
            return
        if parsed.path == "/admin/disputes":
            if not self._require_admin(parsed.query):
                return
            params = parse_qs(parsed.query)
            status = params.get("status", [None])[0]
            self._send_json({"disputes": self.server.app.disputes.list_disputes(status=status, limit=50)})
            return
        if parsed.path.startswith("/admin/disputes/"):
            if not self._require_admin(parsed.query):
                return
            dispute_id = parsed.path.removeprefix("/admin/disputes/").strip("/")
            try:
                detail = self._dispute_detail(dispute_id)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if self._wants_json():
                self._send_json({"dispute": detail})
            else:
                self._send_html(self._render_detail("争议详情", detail, parsed.query))
            return
        if parsed.path == "/admin/topups":
            if not self._require_admin(parsed.query):
                return
            params = parse_qs(parsed.query)
            status = params.get("status", [None])[0]
            self._send_json({"topup_requests": self.server.app.topup_approvals.list_requests(status=status, limit=50)})
            return
        if parsed.path == "/admin/deliveries":
            if not self._require_admin(parsed.query):
                return
            params = parse_qs(parsed.query)
            status = params.get("status", [None])[0]
            self._send_json({"deliveries": self._list_deliveries(status=status)})
            return
        if parsed.path.startswith("/admin/deliveries/"):
            if not self._require_admin(parsed.query):
                return
            delivery_id = parsed.path.removeprefix("/admin/deliveries/").strip("/")
            try:
                detail = self._delivery_detail(delivery_id)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if self._wants_json():
                self._send_json({"delivery": detail})
            else:
                self._send_html(self._render_detail("投放详情", detail, parsed.query))
            return
        self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._check_content_length():
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/telegram/webhook"):
            self._handle_telegram_webhook(parsed.path)
            return
        if not self._require_admin(parsed.query):
            return
        data = self._read_body()
        try:
            result = self._handle_admin_action(parsed.path, data)
        except (ChaboError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception:
            logger.exception("admin_action_failed path=%s", parsed.path)
            self._send_json({"ok": False, "error": "internal error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if self._wants_json():
            self._send_json({"ok": True, "result": result})
        else:
            token_query = self._admin_token_query(parsed.query)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/admin{token_query}")
            self.end_headers()

    def _handle_telegram_webhook(self, path: str) -> None:
        expected = self.server.webhook_secret
        if expected:
            prefix = "/telegram/webhook/"
            path_secret = path[len(prefix):] if path.startswith(prefix) else None
            header_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if not (_safe_eq(path_secret, expected) or _safe_eq(header_secret, expected)):
                self._send_json({"ok": False, "error": "invalid webhook secret"}, HTTPStatus.FORBIDDEN)
                return
        update = self._read_body()
        if not isinstance(update, dict):
            self._send_json({"ok": False, "error": "invalid update"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            result = self.server.app.update_handler.handle(update)
        except Exception:
            logger.exception("webhook_update_failed")
            self._send_json({"ok": False, "error": "webhook handler failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "result": result})

    def _handle_admin_action(self, path: str, data: dict[str, Any]) -> Any:
        parts = [part for part in path.split("/") if part]
        if parts == ["admin", "dispatch-due"]:
            return self.server.app.fulfillment.dispatch_due(_int_value(data, "limit", 20))
        if parts == ["admin", "confirm-earnings"]:
            count = self.server.app.fulfillment.confirm_due_earnings(_int_value(data, "observation_hours", 24))
            return {"confirmed_deliveries": count}
        note = _str_value(data, "note", "") or None
        if len(parts) == 4 and parts[0] == "admin" and parts[1] == "orders" and parts[3] == "approve":
            return self.server.app.orders.approve_order(parts[2], note=note)
        if len(parts) == 4 and parts[0] == "admin" and parts[1] == "orders" and parts[3] == "reject":
            return self.server.app.orders.reject_order(parts[2], _str_value(data, "reason", "运营审核拒绝"), note=note)
        if len(parts) == 4 and parts[0] == "admin" and parts[1] == "deliveries" and parts[3] == "refund":
            amount_cents = _optional_money_cents(data, "amount")
            if amount_cents is None:
                return self.server.app.orders.refund_delivery(parts[2], _str_value(data, "reason", "运营后台退款"), note=note)
            return self.server.app.orders.refund_delivery_partial(parts[2], amount_cents, _str_value(data, "reason", "运营后台部分退款"), note=note)
        if len(parts) == 4 and parts[0] == "admin" and parts[1] == "disputes" and parts[3] == "resolve":
            return self.server.app.disputes.resolve_dispute(
                dispute_id=parts[2],
                resolution=_str_value(data, "resolution", "运营裁决通过"),
                note=note,
            )
        if len(parts) == 4 and parts[0] == "admin" and parts[1] == "topups" and parts[3] == "approve":
            return self.server.app.topup_approvals.approve_topup(
                request_id=parts[2],
                approver_telegram_user_id=_str_value(data, "approver_telegram_user_id", ""),
                approval_note=note,
            )
        if len(parts) == 4 and parts[0] == "admin" and parts[1] == "topups" and parts[3] == "reject":
            return self.server.app.topup_approvals.reject_topup(
                request_id=parts[2],
                approver_telegram_user_id=_str_value(data, "approver_telegram_user_id", ""),
                approval_note=note,
            )
        raise ValueError(f"unknown admin action: {path}")

    def _render_admin(self, query: str) -> str:
        token_query = self._admin_token_query(query)
        params = parse_qs(query)
        order_status = params.get("status", [None])[0]
        orders = self._list_orders(status=order_status, limit=20)
        deliveries = self._list_deliveries(status=None, limit=20)
        disputes = self.server.app.disputes.list_disputes(status=None, limit=20)
        topup_requests = self.server.app.topup_approvals.list_requests(status="pending", limit=20)
        summary = self._ops_summary()
        rows = [
            "<!doctype html><html><head><meta charset='utf-8'><title>插播运营后台</title>",
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;background:#f6f7f9;color:#17202a}"
            "h1{font-size:26px;margin:0 0 20px}h2{font-size:18px;margin:28px 0 10px}"
            "table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dde2e8}"
            "th,td{border-bottom:1px solid #edf0f3;padding:8px;text-align:left;font-size:13px;vertical-align:top}"
            "th{background:#eef2f6}button{padding:6px 10px;border:1px solid #bac3cf;background:#fff;border-radius:6px;cursor:pointer}"
            "input{padding:6px;border:1px solid #bac3cf;border-radius:6px}.muted{color:#697586}.bar form{display:inline;margin-right:8px}"
            ".summary{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 20px}"
            ".summary-card{background:#fff;border:1px solid #dde2e8;border-radius:10px;padding:14px 18px;min-width:140px}"
            ".summary-card .label{color:#697586;font-size:12px;letter-spacing:.5px;text-transform:uppercase}"
            ".summary-card .value{font-size:24px;font-weight:600;margin-top:4px}"
            ".summary-card.alert{border-color:#ec7d6c;background:#fff6f4}"
            ".summary-card.alert .value{color:#c0392b}</style></head><body>",
            "<h1>插播运营后台</h1>",
            "<div class='summary'>",
            self._summary_card("待审核订单", summary["pending_review_orders"], alert=summary["pending_review_orders"] > 0),
            self._summary_card("进行中订单", summary["running_orders"]),
            self._summary_card("今日已发", summary["sent_today"]),
            self._summary_card("到期未发", summary["scheduled_due"], alert=summary["scheduled_due"] > 0),
            self._summary_card("Open 争议", summary["open_disputes"], alert=summary["open_disputes"] > 0),
            self._summary_card("近 24h 失败", summary["failed_recent"], alert=summary["failed_recent"] > 0),
            self._summary_card("待审入账", summary["pending_topups"], alert=summary["pending_topups"] > 0),
            "</div>",
            "<div class='bar'>",
            f"<a href='/admin{token_query}'>全部</a> ",
            f"<a href='{_e(_url_with_params('/admin', token_query, {'status': 'pending_review'}))}'>待审核</a> ",
            f"<a href='{_e(_url_with_params('/admin/disputes', token_query, {'status': 'open'}))}'>Open 争议 JSON</a> ",
            self._form("/admin/dispatch-due", token_query, "<input name='limit' value='20' size='4'><button>调度到期插播</button>"),
            self._form("/admin/confirm-earnings", token_query, "<input name='observation_hours' value='24' size='4'><button>确认到期收益</button>"),
            "</div>",
            "<h2>待审入账</h2>",
            self._topup_requests_table(topup_requests, token_query),
            "<h2>插播订单</h2>",
            self._orders_table(orders, token_query),
            "<h2>投放记录</h2>",
            self._deliveries_table(deliveries, token_query),
            "<h2>争议</h2>",
            self._disputes_table(disputes, token_query),
            "</body></html>",
        ]
        return "".join(rows)

    def _orders_table(self, orders: list[dict[str, Any]], token_query: str) -> str:
        head = "<table><tr><th>订单</th><th>频道</th><th>状态</th><th>预算</th><th>单价</th><th>文案</th><th>动作</th></tr>"
        rows = []
        for order in orders:
            actions = ""
            detail_link = f"<a href='/admin/orders/{_e(order['id'])}{token_query}'>详情</a>"
            if order["status"] in {"pending_review", "paused"}:
                actions += self._form(
                    f"/admin/orders/{order['id']}/approve",
                    token_query,
                    "<input name='note' placeholder='备注（可空）' size='14'><button>通过</button>",
                )
                actions += self._form(
                    f"/admin/orders/{order['id']}/reject",
                    token_query,
                    "<input name='reason' value='素材不符合插播规范'>"
                    "<input name='note' placeholder='备注（可空）' size='14'><button>拒绝</button>",
                )
            action_cell = detail_link + (actions or "")
            rows.append(
                "<tr>"
                f"<td>{_e(order['id'])}<br><span class='muted'>{_e(order['created_at'])}</span></td>"
                f"<td>{_e(order['channel_title'])}</td>"
                f"<td>{_e(order['status'])}</td>"
                f"<td>{_money(order['budget_cents'])}<br>预留 {_money(order['reserved_cents'])}<br>已花 {_money(order['spent_cents'])}</td>"
                f"<td>{_money(order['unit_price_cents'])}</td>"
                f"<td>{_e(order['creative_text'])}<br><span class='muted'>{_e(order['target_url'])}</span></td>"
                f"<td>{action_cell}</td>"
                "</tr>"
            )
        return head + "".join(rows) + "</table>"

    def _deliveries_table(self, deliveries: list[dict[str, Any]], token_query: str) -> str:
        head = "<table><tr><th>投放</th><th>频道</th><th>状态</th><th>消息</th><th>收费</th><th>动作</th></tr>"
        rows = []
        for delivery in deliveries:
            actions = ""
            detail_link = f"<a href='/admin/deliveries/{_e(delivery['id'])}{token_query}'>详情</a>"
            if delivery["status"] in {"sent", "disputed"}:
                actions = self._form(
                    f"/admin/deliveries/{delivery['id']}/refund",
                    token_query,
                    "<input name='amount' placeholder='留空全退' size='8'>"
                    "<input name='reason' value='运营退款'>"
                    "<input name='note' placeholder='备注（可空）' size='14'><button>退款</button>",
                )
            action_cell = detail_link + (actions or "")
            rows.append(
                "<tr>"
                f"<td>{_e(delivery['id'])}<br><span class='muted'>{_e(delivery['scheduled_at'])}</span></td>"
                f"<td>{_e(delivery['channel_title'])}</td>"
                f"<td>{_e(delivery['status'])}</td>"
                f"<td>{_e(delivery.get('message_id') or '')}</td>"
                f"<td>{_money(delivery['charge_cents'])}<br>已退 {_money(delivery['refunded_cents'])}</td>"
                f"<td>{action_cell}</td>"
                "</tr>"
            )
        return head + "".join(rows) + "</table>"

    def _topup_requests_table(self, requests: list[dict[str, Any]], token_query: str) -> str:
        if not requests:
            return "<p class='muted'>没有待审的入账请求。</p>"
        head = (
            "<table><tr><th>请求</th><th>收款方</th><th>金额</th><th>原因</th>"
            "<th>申请人 / 凭证</th><th>动作（审批人 ≠ 申请人）</th></tr>"
        )
        rows = []
        for request in requests:
            actions = self._form(
                f"/admin/topups/{request['id']}/approve",
                token_query,
                "<input name='approver_telegram_user_id' placeholder='审批人 TG ID' size='12'>"
                "<input name='note' placeholder='备注（可空）' size='14'>"
                "<button>通过并入账</button>",
            )
            actions += self._form(
                f"/admin/topups/{request['id']}/reject",
                token_query,
                "<input name='approver_telegram_user_id' placeholder='审批人 TG ID' size='12'>"
                "<input name='note' placeholder='备注（可空）' size='14'>"
                "<button>拒绝</button>",
            )
            evidence_html = (
                f"<a href='{_e(request['evidence_url'])}' target='_blank'>凭证</a>"
                if request.get("evidence_url") else "<span class='muted'>无凭证</span>"
            )
            rows.append(
                "<tr>"
                f"<td>{_e(request['id'])}<br><span class='muted'>{_e(request['created_at'])}</span></td>"
                f"<td>{_e(request['recipient_telegram_user_id'])}</td>"
                f"<td>{request['currency']} {_money(request['amount_cents'])}</td>"
                f"<td>{_e(request['reason'])}</td>"
                f"<td>{_e(request['requester_account_id'])}<br>{evidence_html}</td>"
                f"<td>{actions}</td>"
                "</tr>"
            )
        return head + "".join(rows) + "</table>"

    def _disputes_table(self, disputes: list[dict[str, Any]], token_query: str) -> str:
        head = "<table><tr><th>争议</th><th>频道</th><th>状态</th><th>原因</th><th>动作</th></tr>"
        rows = []
        for dispute in disputes:
            actions = ""
            detail_link = f"<a href='/admin/disputes/{_e(dispute['id'])}{token_query}'>详情</a>"
            if dispute["status"] == "open":
                actions = self._form(
                    f"/admin/disputes/{dispute['id']}/resolve",
                    token_query,
                    "<input name='resolution' value='运营裁决通过'>"
                    "<input name='note' placeholder='备注（可空）' size='14'><button>解决</button>",
                )
            action_cell = detail_link + (actions or "")
            rows.append(
                "<tr>"
                f"<td>{_e(dispute['id'])}<br><span class='muted'>{_e(dispute['created_at'])}</span></td>"
                f"<td>{_e(dispute.get('channel_title') or '')}</td>"
                f"<td>{_e(dispute['status'])}</td>"
                f"<td>{_e(dispute['reason'])}</td>"
                f"<td>{action_cell}</td>"
                "</tr>"
            )
        return head + "".join(rows) + "</table>"

    def _render_detail(self, title: str, detail: dict[str, Any], query: str) -> str:
        token_query = self._admin_token_query(query)
        rows = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            f"<title>插播{_e(title)}</title>",
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;background:#f6f7f9;color:#17202a}"
            "h1{font-size:24px;margin:0 0 16px}h2{font-size:18px;margin:24px 0 8px}"
            "table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dde2e8;margin-bottom:16px}"
            "th,td{border-bottom:1px solid #edf0f3;padding:8px;text-align:left;font-size:13px;vertical-align:top}"
            "th{background:#eef2f6;width:220px}pre{white-space:pre-wrap;margin:0}.muted{color:#697586}</style></head><body>",
            f"<p><a href='/admin{token_query}'>返回运营后台</a></p>",
            f"<h1>插播{_e(title)}</h1>",
        ]
        for section, value in detail.items():
            rows.append(f"<h2>{_e(section)}</h2>")
            if isinstance(value, list):
                rows.append(self._records_table(value))
            elif isinstance(value, dict):
                rows.append(self._record_table(value))
            else:
                rows.append(f"<pre>{_e(value)}</pre>")
        rows.append("</body></html>")
        return "".join(rows)

    def _record_table(self, row: dict[str, Any]) -> str:
        body = []
        for key, value in row.items():
            body.append(f"<tr><th>{_e(key)}</th><td>{self._format_detail_value(value)}</td></tr>")
        return "<table>" + "".join(body) + "</table>"

    def _records_table(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "<p class='muted'>暂无记录</p>"
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        head = "<tr>" + "".join(f"<th>{_e(key)}</th>" for key in keys) + "</tr>"
        body = []
        for row in rows:
            body.append("<tr>" + "".join(f"<td>{self._format_detail_value(row.get(key, ''))}</td>" for key in keys) + "</tr>")
        return "<table>" + head + "".join(body) + "</table>"

    def _format_detail_value(self, value: Any) -> str:
        if isinstance(value, str) and value.startswith("{"):
            try:
                parsed = json.loads(value)
                return f"<pre>{_e(json.dumps(parsed, ensure_ascii=False, indent=2))}</pre>"
            except json.JSONDecodeError:
                pass
        return _e(value)

    def _fetch_audit_rows(
        self,
        conn,
        *,
        order_ids: list[str] | None = None,
        delivery_ids: list[str] | None = None,
        dispute_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if order_ids:
            placeholders = ",".join("?" * len(order_ids))
            clauses.append(f"(entity_type = 'ad_order' AND entity_id IN ({placeholders}))")
            params.extend(order_ids)
        if delivery_ids:
            placeholders = ",".join("?" * len(delivery_ids))
            clauses.append(f"(entity_type = 'delivery' AND entity_id IN ({placeholders}))")
            params.extend(delivery_ids)
        if dispute_ids:
            placeholders = ",".join("?" * len(dispute_ids))
            clauses.append(f"(entity_type = 'dispute' AND entity_id IN ({placeholders}))")
            params.extend(dispute_ids)
        if not clauses:
            return []
        rows = conn.execute(
            f"SELECT * FROM audit_logs WHERE {' OR '.join(clauses)} ORDER BY created_at DESC",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def _build_timeline(
        self,
        audit_rows: list[dict[str, Any]],
        evidence_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in audit_rows:
            payload: dict[str, Any] = {}
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {"raw": row.get("payload_json") or ""}
            note = payload.get("note") or ""
            summary_bits: list[str] = []
            for key in ("reason", "resolution"):
                if payload.get(key):
                    summary_bits.append(f"{key}={payload[key]}")
            if "refund_cents" in payload:
                summary_bits.append(f"refund_cents={payload['refund_cents']}")
            events.append(
                {
                    "kind": "操作",
                    "at": row.get("created_at") or "",
                    "title": row.get("action") or "",
                    "entity": f"{row.get('entity_type')}#{row.get('entity_id')}",
                    "actor": row.get("actor_account_id") or "(未指定)",
                    "note": note,
                    "summary": "；".join(summary_bits),
                }
            )
        for row in evidence_rows:
            events.append(
                {
                    "kind": "证据",
                    "at": row.get("created_at") or "",
                    "title": row.get("snapshot_type") or "",
                    "entity": f"order#{row.get('order_id')}" if row.get("order_id") else f"delivery#{row.get('delivery_id')}",
                    "actor": "",
                    "note": "",
                    "summary": (row.get("payload_json") or "")[:120],
                }
            )
        events.sort(key=lambda e: e["at"], reverse=True)
        return events

    def _summary_card(self, label: str, value: int, *, alert: bool = False) -> str:
        css = "summary-card alert" if alert else "summary-card"
        return f"<div class='{css}'><div class='label'>{_e(label)}</div><div class='value'>{value}</div></div>"

    def _ops_summary(self) -> dict[str, Any]:
        with self.server.app.db.transaction() as conn:
            pending_review = conn.execute(
                "SELECT COUNT(*) AS n FROM ad_orders WHERE status = 'pending_review'"
            ).fetchone()["n"]
            running_orders = conn.execute(
                "SELECT COUNT(*) AS n FROM ad_orders WHERE status = 'running'"
            ).fetchone()["n"]
            sent_today = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status IN ('sent', 'confirmed') "
                "AND DATE(sent_at) = DATE('now')"
            ).fetchone()["n"]
            open_disputes = conn.execute(
                "SELECT COUNT(*) AS n FROM disputes WHERE status = 'open'"
            ).fetchone()["n"]
            failed_recent = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'failed' "
                "AND updated_at >= datetime('now', '-1 day')"
            ).fetchone()["n"]
            scheduled_due = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'scheduled' "
                "AND scheduled_at <= datetime('now')"
            ).fetchone()["n"]
            pending_topups = conn.execute(
                "SELECT COUNT(*) AS n FROM topup_requests WHERE status = 'pending'"
            ).fetchone()["n"]
        return {
            "pending_review_orders": pending_review,
            "running_orders": running_orders,
            "sent_today": sent_today,
            "open_disputes": open_disputes,
            "failed_recent": failed_recent,
            "scheduled_due": scheduled_due,
            "pending_topups": pending_topups,
        }

    def _build_health_payload(self) -> dict[str, Any]:
        settings = self.server.app.settings
        payload: dict[str, Any] = {
            "ok": True,
            "service": "chabo",
            "bot_username": settings.bot_username,
            "db_path": settings.db_path,
        }
        try:
            with self.server.app.db.transaction() as conn:
                conn.execute("SELECT 1").fetchone()
            payload["db"] = "ok"
            payload["ops"] = self._ops_summary()
        except Exception as exc:
            payload["ok"] = False
            payload["db"] = "error"
            payload["db_error"] = str(exc)[:200]
        return payload

    def _form(self, path: str, token_query: str, body: str) -> str:
        token_input = ""
        if self.server.admin_token:
            token_input = f"<input type='hidden' name='token' value='{_e(self.server.admin_token)}'>"
        return f"<form method='post' action='{_e(path)}{token_query}'>{token_input}{body}</form>"

    def _list_orders(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE o.status = ?"
            params.append(status)
        params.append(max(1, min(limit, 100)))
        with self.server.app.db.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT o.*, c.title AS channel_title, cr.text AS creative_text, cr.target_url
                FROM ad_orders o
                JOIN channels c ON c.id = o.channel_id
                JOIN creatives cr ON cr.id = o.creative_id
                {where}
                ORDER BY o.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def _list_deliveries(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE d.status = ?"
            params.append(status)
        params.append(max(1, min(limit, 100)))
        with self.server.app.db.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT d.*, c.title AS channel_title, o.status AS order_status
                FROM deliveries d
                JOIN channels c ON c.id = d.channel_id
                JOIN ad_orders o ON o.id = d.order_id
                {where}
                ORDER BY d.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def _order_detail(self, order_id: str) -> dict[str, Any]:
        with self.server.app.db.transaction() as conn:
            order = conn.execute(
                """
                SELECT o.*, c.title AS channel_title, c.telegram_chat_id, cr.text AS creative_text,
                       cr.target_url, cr.button_text, cr.category, a.telegram_user_id AS advertiser_telegram_user_id
                FROM ad_orders o
                JOIN channels c ON c.id = o.channel_id
                JOIN creatives cr ON cr.id = o.creative_id
                JOIN accounts a ON a.id = o.advertiser_account_id
                WHERE o.id = ?
                """,
                (order_id,),
            ).fetchone()
            if not order:
                raise KeyError(f"order not found: {order_id}")
            deliveries = conn.execute("SELECT * FROM deliveries WHERE order_id = ? ORDER BY created_at DESC", (order_id,)).fetchall()
            evidence = conn.execute("SELECT * FROM evidence_snapshots WHERE order_id = ? ORDER BY created_at DESC", (order_id,)).fetchall()
            ledger = conn.execute("SELECT * FROM ledger_transactions WHERE order_id = ? ORDER BY created_at DESC", (order_id,)).fetchall()
            delivery_ids = [row["id"] for row in deliveries]
            audit_rows = self._fetch_audit_rows(conn, order_ids=[order_id], delivery_ids=delivery_ids)
            timeline = self._build_timeline(audit_rows, [dict(row) for row in evidence])
            return {
                "订单": dict(order),
                "投放": [dict(row) for row in deliveries],
                "时间线": timeline,
                "账本流水": [dict(row) for row in ledger],
            }

    def _delivery_detail(self, delivery_id: str) -> dict[str, Any]:
        with self.server.app.db.transaction() as conn:
            delivery = conn.execute(
                """
                SELECT d.*, o.status AS order_status, o.budget_cents, o.reserved_cents, o.spent_cents,
                       c.title AS channel_title, c.telegram_chat_id, cr.text AS creative_text, cr.target_url
                FROM deliveries d
                JOIN ad_orders o ON o.id = d.order_id
                JOIN channels c ON c.id = d.channel_id
                JOIN creatives cr ON cr.id = d.creative_id
                WHERE d.id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if not delivery:
                raise KeyError(f"delivery not found: {delivery_id}")
            disputes = conn.execute("SELECT * FROM disputes WHERE delivery_id = ? ORDER BY created_at DESC", (delivery_id,)).fetchall()
            evidence = conn.execute("SELECT * FROM evidence_snapshots WHERE delivery_id = ? ORDER BY created_at DESC", (delivery_id,)).fetchall()
            ledger = conn.execute("SELECT * FROM ledger_transactions WHERE delivery_id = ? ORDER BY created_at DESC", (delivery_id,)).fetchall()
            audit_rows = self._fetch_audit_rows(
                conn,
                delivery_ids=[delivery_id],
                dispute_ids=[row["id"] for row in disputes],
            )
            timeline = self._build_timeline(audit_rows, [dict(row) for row in evidence])
            return {
                "投放": dict(delivery),
                "争议": [dict(row) for row in disputes],
                "时间线": timeline,
                "账本流水": [dict(row) for row in ledger],
            }

    def _dispute_detail(self, dispute_id: str) -> dict[str, Any]:
        with self.server.app.db.transaction() as conn:
            dispute = conn.execute(
                """
                SELECT d.*, c.title AS channel_title, del.status AS delivery_status,
                       del.message_id, o.status AS order_status
                FROM disputes d
                JOIN deliveries del ON del.id = d.delivery_id
                JOIN ad_orders o ON o.id = d.order_id
                JOIN channels c ON c.id = del.channel_id
                WHERE d.id = ?
                """,
                (dispute_id,),
            ).fetchone()
            if not dispute:
                raise KeyError(f"dispute not found: {dispute_id}")
            evidence = conn.execute(
                """
                SELECT *
                FROM evidence_snapshots
                WHERE order_id = ? OR delivery_id = ?
                ORDER BY created_at DESC
                """,
                (dispute["order_id"], dispute["delivery_id"]),
            ).fetchall()
            ledger = conn.execute(
                "SELECT * FROM ledger_transactions WHERE order_id = ? ORDER BY created_at DESC",
                (dispute["order_id"],),
            ).fetchall()
            audit_rows = self._fetch_audit_rows(
                conn,
                order_ids=[dispute["order_id"]] if dispute["order_id"] else None,
                delivery_ids=[dispute["delivery_id"]] if dispute["delivery_id"] else None,
                dispute_ids=[dispute_id],
            )
            timeline = self._build_timeline(audit_rows, [dict(row) for row in evidence])
            return {
                "争议": dict(dispute),
                "时间线": timeline,
                "账本流水": [dict(row) for row in ledger],
            }

    def _require_admin(self, query: str) -> bool:
        token = self.server.admin_token
        if not token:
            self._send_json({"ok": False, "error": "CHABO_ADMIN_TOKEN is required for admin endpoints"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return False
        params = parse_qs(query)
        provided = params.get("token", [None])[0]
        bearer = self.headers.get("Authorization", "")
        bearer_token = bearer[len("Bearer "):] if bearer.startswith("Bearer ") else None
        header_token = self.headers.get("X-Chabo-Admin-Token")
        if _safe_eq(provided, token) or _safe_eq(header_token, token) or _safe_eq(bearer_token, token):
            return True
        self._send_json({"ok": False, "error": "admin token required"}, HTTPStatus.UNAUTHORIZED)
        return False

    def _check_content_length(self) -> bool:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            length = 0
        if length > MAX_REQUEST_BYTES:
            self._send_json(
                {"ok": False, "error": "request body too large"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return False
        return True

    def _admin_token_query(self, query: str) -> str:
        token = parse_qs(query).get("token", [None])[0]
        if token:
            return "?" + urlencode({"token": token})
        return ""

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(raw.decode("utf-8") or "{}")
        if "application/x-www-form-urlencoded" in content_type:
            parsed = parse_qs(raw.decode("utf-8"))
            return {key: values[-1] if values else "" for key, values in parsed.items()}
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _wants_json(self) -> bool:
        accept = self.headers.get("Accept", "")
        return "application/json" in accept or self.headers.get("Content-Type", "").startswith("application/json")

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _int_value(data: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _str_value(data: dict[str, Any], key: str, default: str) -> str:
    value = str(data.get(key) or "").strip()
    return value or default


def _optional_money_cents(data: dict[str, Any], key: str) -> int | None:
    value = str(data.get(key) or "").strip()
    if not value:
        return None
    return money_to_cents(value)


def _money(cents: int) -> str:
    return f"USD {cents_to_money(cents)}"


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _url_with_params(path: str, token_query: str, extra: dict[str, str]) -> str:
    params = parse_qs(token_query.removeprefix("?")) if token_query else {}
    for key, value in extra.items():
        params[key] = [value]
    return f"{path}?{urlencode({key: values[-1] for key, values in params.items()})}"
