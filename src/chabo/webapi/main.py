from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..app import ChaboApp, create_app
from ..audit import insert_audit_log, latest_audit_hash, verify_audit_chain
from ..config import Settings
from ..ids import new_id
from ..services import ChaboError, NotFound
from .auth import (
    ADMIN_LEVEL_ORDER,
    admin_level,
    active_portals,
    build_magic_link_url,
    consume_login_token,
    grant_portal,
    issue_impersonation_session,
    issue_login_token,
    issue_session,
    magic_login_path,
    portal_statuses,
    revoke_portal,
    stop_impersonation_session,
    sync_portal_access_from_activity,
    validate_telegram_init_data,
)
from .deps import current_session, get_chabo_app, require_admin, require_admin_level, require_portal
from .readmodels import (
    admin_accounts,
    admin_audit_logs,
    admin_channels,
    admin_delivery_detail,
    admin_deliveries,
    admin_dispute_detail,
    admin_disputes,
    admin_order_detail,
    admin_orders,
    admin_settings,
    admin_topups,
    admin_wallet,
    advertiser_dashboard,
    advertiser_orders,
    channel_market,
    ops_summary,
    publisher_dashboard,
    publisher_channel_deliveries,
    publisher_channels,
)
from .plans import add_plan_items, create_plan, get_plan, list_plans, submit_plan
from .schemas import (
    DailyLimitRequest,
    DevSessionRequest,
    FormatPolicyRequest,
    MagicConsumeRequest,
    ImpersonationStartRequest,
    MagicLinkRequest,
    MaterialCreateRequest,
    NoteRequest,
    PlanAddItemsRequest,
    PlanCreateRequest,
    PlanSubmitRequest,
    PortalAccessUpdateRequest,
    RateRequest,
    RefundDeliveryRequest,
    RejectOrderRequest,
    ResolveDisputeRequest,
    TelegramWebAppLoginRequest,
    TopupCreateRequest,
)


PERMISSION_CONFIRM_PHRASE = "确认调整权限"


def _portal_access_snapshot(conn: Any, account_id: str, portal: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT portal, status, grant_reason, granted_at, revoked_at, updated_at, metadata_json
        FROM portal_access
        WHERE account_id = ? AND portal = ?
        """,
        (account_id, portal),
    ).fetchone()
    if not row:
        return None
    snapshot = dict(row)
    metadata_raw = snapshot.pop("metadata_json", None)
    try:
        metadata = json.loads(metadata_raw or "{}")
    except json.JSONDecodeError:
        metadata = {}
    snapshot["admin_level"] = metadata.get("admin_level") if portal == "admin" else None
    return snapshot


def _snapshot_diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    fields = sorted(set((before or {}).keys()) | set((after or {}).keys()))
    return {
        field: {"before": (before or {}).get(field), "after": (after or {}).get(field)}
        for field in fields
        if (before or {}).get(field) != (after or {}).get(field)
    }


def create_web_api(settings: Settings | None = None, chabo: ChaboApp | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    chabo = chabo or create_app(settings)
    api = FastAPI(title="Chabo Web API", version="0.1.0")
    api.state.chabo = chabo
    api.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.web_allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(api)
    register_routes(api)
    return api


def set_session_cookie(response: Response, chabo: ChaboApp, token: str) -> None:
    response.set_cookie(
        chabo.settings.session_cookie_name,
        token,
        httponly=True,
        samesite=chabo.settings.session_cookie_samesite,
        secure=chabo.settings.session_cookie_secure,
        max_age=14 * 24 * 3600,
    )


def delete_session_cookie(response: Response, chabo: ChaboApp) -> None:
    response.delete_cookie(
        chabo.settings.session_cookie_name,
        samesite=chabo.settings.session_cookie_samesite,
        secure=chabo.settings.session_cookie_secure,
    )


def register_exception_handlers(api: FastAPI) -> None:
    @api.exception_handler(NotFound)
    def not_found_handler(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(content={"detail": str(exc)}, status_code=404)

    @api.exception_handler(ChaboError)
    def chabo_error_handler(request: Request, exc: ChaboError) -> JSONResponse:
        return JSONResponse(content={"detail": str(exc)}, status_code=400)


def register_routes(api: FastAPI) -> None:
    @api.get("/api/health")
    def health(chabo: ChaboApp = Depends(get_chabo_app)) -> dict[str, Any]:
        with chabo.db.transaction() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "service": "chabo-api", "ops": ops_summary(chabo)}

    @api.get("/api/auth/config")
    def auth_config(chabo: ChaboApp = Depends(get_chabo_app)) -> dict[str, Any]:
        return {
            "environment": chabo.settings.environment,
            "telegram_webapp_available": bool(chabo.settings.bot_token),
            "dev_auth_bypass": chabo.settings.dev_auth_bypass and not chabo.settings.is_production,
            "dev_session_enabled": chabo.settings.dev_session_enabled and not chabo.settings.is_production,
        }

    @api.post("/api/auth/dev-session")
    def dev_session(
        payload: DevSessionRequest,
        request: Request,
        response: Response,
        chabo: ChaboApp = Depends(get_chabo_app),
        x_chabo_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if chabo.settings.is_production or not chabo.settings.dev_session_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        if not chabo.settings.admin_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CHABO_ADMIN_TOKEN required for dev session bootstrap",
            )
        if x_chabo_admin_token != chabo.settings.admin_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_admin_token")
        role = "publisher" if payload.portals == ["publisher"] else "advertiser"
        if len(payload.portals) > 1:
            role = "mixed"
        with chabo.db.transaction() as conn:
            account = chabo.ledger.accounts.get_or_create_by_telegram(
                conn,
                payload.telegram_user_id,
                role,
                payload.display_name,
            )
            for portal in payload.portals:
                grant_portal(
                    conn,
                    account_id=account["id"],
                    portal=portal,
                    status="active",
                    reason="dev_session",
                    metadata={"admin_level": "super_admin"} if portal == "admin" else None,
                )
            activation = sync_portal_access_from_activity(conn, account["id"])
            portals = active_portals(conn, account["id"])
            statuses = portal_statuses(conn, account["id"])
            level = admin_level(conn, account["id"])
        token, session = issue_session(
            chabo,
            account_id=account["id"],
            source="dev_session",
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
        set_session_cookie(response, chabo, token)
        return {
            "session_id": session["id"],
            "account": account,
            "portals": portals,
            "portal_statuses": statuses,
            "admin_level": level,
            "activation": activation,
            "token": token,
        }

    @api.post("/api/auth/telegram-webapp")
    def telegram_webapp_login(
        payload: TelegramWebAppLoginRequest,
        request: Request,
        response: Response,
        chabo: ChaboApp = Depends(get_chabo_app),
    ) -> dict[str, Any]:
        if not chabo.settings.bot_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CHABO_BOT_TOKEN required for Telegram WebApp login",
            )
        try:
            verified = validate_telegram_init_data(
                payload.init_data,
                bot_token=chabo.settings.bot_token,
                max_age_seconds=chabo.settings.telegram_webapp_max_age_seconds,
            )
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        user = verified["user"]
        display_name = user.get("username") or " ".join(
            part for part in [user.get("first_name"), user.get("last_name")] if part
        ) or None
        with chabo.db.transaction() as conn:
            account = chabo.ledger.accounts.get_or_create_by_telegram(
                conn,
                str(user["id"]),
                "advertiser",
                display_name,
            )
            activation = sync_portal_access_from_activity(conn, account["id"], ensure_login_candidates=True)
            portals = active_portals(conn, account["id"])
            statuses = portal_statuses(conn, account["id"])
        token, session = issue_session(
            chabo,
            account_id=account["id"],
            source="telegram_webapp",
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
        set_session_cookie(response, chabo, token)
        return {
            "session_id": session["id"],
            "account": account,
            "portals": portals,
            "portal_statuses": statuses,
            "activation": activation,
        }

    @api.post("/api/auth/magic-link")
    def create_magic_link(
        payload: MagicLinkRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        x_chabo_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not chabo.settings.admin_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CHABO_ADMIN_TOKEN required for magic-link bootstrap",
            )
        if x_chabo_admin_token != chabo.settings.admin_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_admin_token")
        role = "mixed" if len(payload.portals) > 1 else (payload.portals[0] if payload.portals else "advertiser")
        if role == "admin":
            role = "advertiser"
        with chabo.db.transaction() as conn:
            account = chabo.ledger.accounts.get_or_create_by_telegram(
                conn,
                payload.telegram_user_id,
                role,
                payload.display_name,
            )
            for portal in payload.portals:
                grant_portal(
                    conn,
                    account_id=account["id"],
                    portal=portal,
                    status="active",
                    reason="magic_link_bootstrap",
                    metadata={"admin_level": "super_admin"} if portal == "admin" else None,
                )
            activation = sync_portal_access_from_activity(conn, account["id"])
            statuses = portal_statuses(conn, account["id"])
        login_token, row = issue_login_token(chabo, account_id=account["id"])
        return {
            "token": login_token,
            "expires_at": row["expires_at"],
            "path": magic_login_path(login_token),
            "url": build_magic_link_url(chabo.settings, login_token),
            "portal_statuses": statuses,
            "activation": activation,
        }

    @api.post("/api/auth/magic/consume")
    def consume_magic_link(
        payload: MagicConsumeRequest,
        request: Request,
        response: Response,
        chabo: ChaboApp = Depends(get_chabo_app),
    ) -> dict[str, Any]:
        try:
            account = consume_login_token(chabo, payload.token)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        with chabo.db.transaction() as conn:
            activation = sync_portal_access_from_activity(conn, account["id"])
            portals = active_portals(conn, account["id"])
            statuses = portal_statuses(conn, account["id"])
        token, session = issue_session(
            chabo,
            account_id=account["id"],
            source="magic_link",
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
        set_session_cookie(response, chabo, token)
        return {
            "session_id": session["id"],
            "account": account,
            "portals": portals,
            "portal_statuses": statuses,
            "activation": activation,
        }

    @api.get("/api/auth/me")
    def me(session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        return {
            "account": {
                "id": session["account_id"],
                "telegram_user_id": session["telegram_user_id"],
                "role": session["role"],
                "display_name": session["display_name"],
            },
            "portals": session.get("portals", []),
            "portal_statuses": session.get("portal_statuses", []),
            "admin_level": session.get("admin_level"),
            "activation": session.get("activation", {}),
            "impersonator_account_id": session.get("impersonator_account_id"),
            "session_expires_at": session.get("expires_at"),
        }

    @api.post("/api/auth/impersonation/stop")
    def stop_impersonation(
        request: Request,
        response: Response,
        session: dict[str, Any] = Depends(current_session),
        chabo: ChaboApp = Depends(get_chabo_app),
    ) -> dict[str, Any]:
        try:
            token, restored_session = stop_impersonation_session(
                chabo,
                session=session,
                user_agent=request.headers.get("user-agent"),
                ip_address=request.client.host if request.client else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        set_session_cookie(response, chabo, token)
        with chabo.db.transaction() as conn:
            activation = sync_portal_access_from_activity(conn, restored_session["account_id"])
            portals = active_portals(conn, restored_session["account_id"])
            statuses = portal_statuses(conn, restored_session["account_id"])
            level = admin_level(conn, restored_session["account_id"])
        return {
            "session_id": restored_session["id"],
            "portals": portals,
            "portal_statuses": statuses,
            "admin_level": level,
            "activation": activation,
        }

    @api.post("/api/auth/logout")
    def logout(
        response: Response,
        session: dict[str, Any] = Depends(current_session),
        chabo: ChaboApp = Depends(get_chabo_app),
    ) -> dict[str, bool]:
        with chabo.db.transaction() as conn:
            conn.execute("UPDATE web_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE id = ?", (session["id"],))
            source = str(session.get("source") or "")
            if session.get("impersonator_account_id") and source.startswith("impersonation:"):
                conn.execute(
                    "UPDATE impersonation_sessions SET ended_at = CURRENT_TIMESTAMP WHERE id = ? AND ended_at IS NULL",
                    (source.split(":", 1)[1],),
                )
        delete_session_cookie(response, chabo)
        return {"ok": True}

    @api.get("/api/admin/summary")
    def admin_summary(
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return {"metrics": ops_summary(chabo), "session": {"account_id": session["account_id"]}}

    @api.post("/api/admin/impersonations")
    def start_admin_impersonation(
        payload: ImpersonationStartRequest,
        request: Request,
        response: Response,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("super_admin")),
    ) -> dict[str, Any]:
        if not payload.target_account_id and not payload.telegram_user_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target_account_id_or_telegram_user_id_required")
        reason = payload.reason.strip()
        if len(reason) < 4:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="impersonation_reason_required")
        with chabo.db.transaction() as conn:
            if payload.target_account_id:
                target = conn.execute("SELECT * FROM accounts WHERE id = ?", (payload.target_account_id,)).fetchone()
            else:
                target = conn.execute(
                    "SELECT * FROM accounts WHERE telegram_user_id = ?",
                    (payload.telegram_user_id,),
                ).fetchone()
            if not target:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="target_account_not_found")
            target_account = dict(target)
        try:
            token, web_session, impersonation = issue_impersonation_session(
                chabo,
                admin_account_id=session["account_id"],
                target_account_id=target_account["id"],
                portal=payload.portal,
                reason=reason,
                user_agent=request.headers.get("user-agent"),
                ip_address=request.client.host if request.client else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        set_session_cookie(response, chabo, token)
        with chabo.db.transaction() as conn:
            activation = sync_portal_access_from_activity(conn, target_account["id"])
            portals = active_portals(conn, target_account["id"])
            statuses = portal_statuses(conn, target_account["id"])
        return {
            "session_id": web_session["id"],
            "session_expires_at": web_session["expires_at"],
            "account": target_account,
            "portals": portals,
            "portal_statuses": statuses,
            "activation": activation,
            "impersonation": impersonation,
        }

    @api.get("/api/admin/orders")
    def list_admin_orders(
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_orders(chabo, status=status, limit=limit, offset=offset)

    @api.get("/api/admin/orders/{order_id}")
    def get_admin_order(
        order_id: str,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_order_detail(chabo, order_id)

    @api.post("/api/admin/orders/{order_id}/approve")
    def approve_admin_order(
        order_id: str,
        payload: NoteRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("operator")),
    ) -> dict[str, Any]:
        return chabo.orders.approve_order(order_id, actor_account_id=session["account_id"], note=payload.note)

    @api.post("/api/admin/orders/{order_id}/reject")
    def reject_admin_order(
        order_id: str,
        payload: RejectOrderRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("operator")),
    ) -> dict[str, Any]:
        return chabo.orders.reject_order(order_id, payload.reason, actor_account_id=session["account_id"], note=payload.note)

    @api.get("/api/admin/topups")
    def list_admin_topups(
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_topups(chabo, status=status, limit=limit, offset=offset)

    @api.get("/api/admin/wallet")
    def get_admin_wallet(
        limit: int = 20,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("finance")),
    ) -> dict[str, Any]:
        return admin_wallet(chabo, limit=limit)

    @api.get("/api/admin/settings")
    def get_admin_settings(
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_settings(chabo)

    @api.post("/api/admin/topups")
    def create_admin_topup(
        payload: TopupCreateRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("finance")),
    ) -> dict[str, Any]:
        if not session.get("telegram_user_id"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="admin telegram_user_id required")
        return chabo.topup_approvals.request_topup(
            recipient_telegram_user_id=payload.recipient_telegram_user_id,
            amount_cents=payload.amount_cents,
            reason=payload.reason,
            requester_telegram_user_id=session["telegram_user_id"],
            evidence_url=payload.evidence_url,
            request_note=payload.request_note,
            session_id=session["id"],
        )

    @api.get("/api/admin/deliveries")
    def list_admin_deliveries(
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_deliveries(chabo, status=status, limit=limit, offset=offset)

    @api.get("/api/admin/deliveries/{delivery_id}")
    def get_admin_delivery(
        delivery_id: str,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_delivery_detail(chabo, delivery_id)

    @api.post("/api/admin/deliveries/{delivery_id}/refund")
    def refund_admin_delivery(
        delivery_id: str,
        payload: RefundDeliveryRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("finance")),
    ) -> dict[str, Any]:
        actor = session["account_id"]
        if payload.amount_cents:
            return chabo.orders.refund_delivery_partial(delivery_id, payload.amount_cents, payload.reason, actor, note=payload.note)
        return chabo.orders.refund_delivery(delivery_id, payload.reason, actor, note=payload.note)

    @api.post("/api/admin/deliveries/{delivery_id}/report-deletion")
    def report_admin_delivery_deletion(
        delivery_id: str,
        payload: NoteRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("operator")),
    ) -> dict[str, Any]:
        return chabo.orders.report_publisher_deletion(delivery_id, actor_account_id=session["account_id"], note=payload.note)

    @api.get("/api/admin/disputes")
    def list_admin_disputes(
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_disputes(chabo, status=status, limit=limit, offset=offset)

    @api.get("/api/admin/disputes/{dispute_id}")
    def get_admin_dispute(
        dispute_id: str,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_dispute_detail(chabo, dispute_id)

    @api.post("/api/admin/disputes/{dispute_id}/resolve")
    def resolve_admin_dispute(
        dispute_id: str,
        payload: ResolveDisputeRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("operator")),
    ) -> dict[str, Any]:
        return chabo.disputes.resolve_dispute(
            dispute_id=dispute_id,
            resolution=payload.resolution,
            actor_account_id=session["account_id"],
            note=payload.note,
        )

    @api.post("/api/admin/topups/{request_id}/approve")
    def approve_admin_topup(
        request_id: str,
        payload: NoteRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("finance")),
    ) -> dict[str, Any]:
        if not session.get("telegram_user_id"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="admin telegram_user_id required")
        return chabo.topup_approvals.approve_topup(
            request_id=request_id,
            approver_telegram_user_id=session["telegram_user_id"],
            approval_note=payload.note,
        )

    @api.post("/api/admin/topups/{request_id}/reject")
    def reject_admin_topup(
        request_id: str,
        payload: NoteRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("finance")),
    ) -> dict[str, Any]:
        if not session.get("telegram_user_id"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="admin telegram_user_id required")
        return chabo.topup_approvals.reject_topup(
            request_id=request_id,
            approver_telegram_user_id=session["telegram_user_id"],
            approval_note=payload.note,
        )

    @api.get("/api/admin/accounts")
    def list_admin_accounts(
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_accounts(chabo, q=q, limit=limit, offset=offset)

    @api.put("/api/admin/accounts/{account_id}/portals/{portal}")
    def update_admin_account_portal(
        account_id: str,
        portal: str,
        payload: PortalAccessUpdateRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("super_admin")),
    ) -> dict[str, Any]:
        if portal not in {"admin", "advertiser", "publisher"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="portal_not_found")
        reason = payload.reason.strip()
        if not reason:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="permission_reason_required")
        high_risk = portal == "admin" or payload.status == "revoked"
        if high_risk and (payload.confirm_phrase or "").strip() != PERMISSION_CONFIRM_PHRASE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="permission_confirm_phrase_required")
        if account_id == session["account_id"] and portal == "admin":
            next_level = payload.admin_level or "viewer"
            if payload.status != "active" or next_level != "super_admin":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot_downgrade_current_super_admin")
        if portal == "admin" and payload.status == "active":
            level = payload.admin_level or "viewer"
            if level not in ADMIN_LEVEL_ORDER:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_admin_level")
            metadata = {"admin_level": level}
        else:
            level = None
            metadata = None
        with chabo.db.transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not account:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account_not_found")
            before_snapshot = _portal_access_snapshot(conn, account_id, portal)
            if payload.status == "revoked":
                access = revoke_portal(
                    conn,
                    account_id=account_id,
                    portal=portal,
                    reason=reason,
                    actor_account_id=session["account_id"],
                    metadata=metadata,
                )
            else:
                access = grant_portal(
                    conn,
                    account_id=account_id,
                    portal=portal,
                    status=payload.status,
                    reason=reason,
                    actor_account_id=session["account_id"],
                    metadata=metadata,
                )
            audit_payload: dict[str, Any] = {
                "portal": portal,
                "status": payload.status,
                "reason": reason,
            }
            if portal == "admin":
                audit_payload["admin_level"] = level
            after_snapshot = _portal_access_snapshot(conn, account_id, portal)
            audit_payload["before"] = before_snapshot
            audit_payload["after"] = after_snapshot
            audit_payload["changed_fields"] = _snapshot_diff(before_snapshot, after_snapshot)
            insert_audit_log(
                conn,
                actor_account_id=session["account_id"],
                action="admin_portal_access_updated",
                entity_type="account",
                entity_id=account_id,
                payload=audit_payload,
            )
            activation = sync_portal_access_from_activity(conn, account_id)
            statuses = portal_statuses(conn, account_id)
            level = admin_level(conn, account_id)
        return {
            "account": dict(account),
            "portal_access": access,
            "portal_statuses": statuses,
            "admin_level": level,
            "activation": activation,
        }

    @api.get("/api/admin/channels")
    def list_admin_channels(
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_channels(chabo, q=q, limit=limit, offset=offset)

    @api.get("/api/admin/audit-logs")
    def list_admin_audit_logs(
        q: str | None = None,
        entity_type: str | None = None,
        category: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        return admin_audit_logs(
            chabo,
            q=q,
            entity_type=entity_type,
            category=category,
            actor=actor,
            target=target,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )

    @api.get("/api/admin/audit-logs/verify")
    def verify_admin_audit_logs(
        created_from: str | None = None,
        created_to: str | None = None,
        strict_unsigned: bool = False,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("super_admin")),
    ) -> dict[str, Any]:
        with chabo.db.transaction() as conn:
            return verify_audit_chain(
                conn,
                created_from=created_from,
                created_to=created_to,
                strict_unsigned=strict_unsigned,
            )

    @api.get("/api/admin/audit-logs/export.csv")
    def export_admin_audit_logs(
        q: str | None = None,
        entity_type: str | None = None,
        category: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        limit: int = 5000,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_admin_level("super_admin")),
    ) -> Response:
        max_rows = max(1, min(chabo.settings.audit_export_max_rows, 5000))
        data = admin_audit_logs(
            chabo,
            q=q,
            entity_type=entity_type,
            category=category,
            actor=actor,
            target=target,
            created_from=created_from,
            created_to=created_to,
            limit=min(limit, max_rows),
            offset=0,
            max_limit=max_rows,
        )
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=[
                "id",
                "created_at",
                "action",
                "entity_type",
                "entity_id",
                "target_telegram_user_id",
                "actor_account_id",
                "actor_telegram_user_id",
                "previous_hash",
                "audit_hash",
                "hash_version",
                "payload_json",
            ],
        )
        writer.writeheader()
        for row in data["items"]:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})
        csv_body = buffer.getvalue()
        csv_sha256 = hashlib.sha256(csv_body.encode("utf-8")).hexdigest()
        with chabo.db.transaction() as conn:
            chain_head_before_export = latest_audit_hash(conn)
            insert_audit_log(
                conn,
                actor_account_id=session["account_id"],
                action="admin_audit_exported",
                entity_type="audit_logs",
                entity_id="export.csv",
                payload={
                    "filters": {
                        "q": q,
                        "entity_type": entity_type,
                        "category": category,
                        "actor": actor,
                        "target": target,
                        "created_from": created_from,
                        "created_to": created_to,
                        "limit": min(limit, max_rows),
                    },
                    "exported_rows": len(data["items"]),
                    "csv_sha256": csv_sha256,
                    "chain_head_before_export": chain_head_before_export,
                },
            )
        return Response(
            content="\ufeff" + csv_body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="chabo-audit-logs.csv"',
                "X-Chabo-Audit-Export-Sha256": csv_sha256,
                "X-Chabo-Audit-Chain-Head": chain_head_before_export or "",
            },
        )

    @api.get("/api/advertiser/dashboard")
    def advertiser_home(
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return advertiser_dashboard(chabo, session["account_id"])

    @api.get("/api/advertiser/channels")
    def advertiser_channels(
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return channel_market(chabo, limit=limit, offset=offset, q=q)

    @api.get("/api/advertiser/materials")
    def advertiser_materials(
        format_type: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        items = chabo.materials.list_materials(
            advertiser_telegram_user_id=session["telegram_user_id"],
            format_type=format_type,
            include_archived=include_archived,
            limit=limit,
        )
        return {"items": items, "total": len(items), "limit": limit, "offset": 0}

    @api.post("/api/advertiser/materials")
    def create_advertiser_material(
        payload: MaterialCreateRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return chabo.materials.create_material(
            advertiser_telegram_user_id=session["telegram_user_id"],
            format_type=payload.format_type,
            text=payload.text,
            target_url=payload.target_url,
            button_text=payload.button_text,
            category=payload.category,
            light_short_text=payload.light_short_text,
            standard_text=payload.standard_text,
            media_file_id=payload.media_file_id,
            media_type=payload.media_type,
            actor_kind="human",
            session_id=session["id"],
        )

    @api.post("/api/advertiser/materials/{material_id}/archive")
    def archive_advertiser_material(
        material_id: str,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return chabo.materials.archive_material(
            material_id,
            advertiser_telegram_user_id=session["telegram_user_id"],
            actor_kind="human",
            session_id=session["id"],
        )

    @api.get("/api/advertiser/orders")
    def list_advertiser_orders(
        status: str | None = None,
        limit: int = 50,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return advertiser_orders(chabo, telegram_user_id=session["telegram_user_id"], status=status, limit=limit)

    @api.get("/api/advertiser/wallet")
    def advertiser_wallet(
        limit: int = 20,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        telegram_user_id = session["telegram_user_id"]
        return {
            "summary": chabo.ledger.get_wallet_summary(telegram_user_id=telegram_user_id),
            "transactions": chabo.ledger.list_transactions(telegram_user_id=telegram_user_id, limit=limit),
            "reserved_orders": chabo.ledger.list_reserved_orders(telegram_user_id=telegram_user_id, limit=limit),
        }

    @api.get("/api/advertiser/plans")
    def list_advertiser_plans(
        limit: int = 50,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        items = list_plans(chabo, advertiser_account_id=session["account_id"], limit=limit)
        return {"items": items, "total": len(items), "limit": limit, "offset": 0}

    @api.post("/api/advertiser/plans")
    def create_advertiser_plan(
        payload: PlanCreateRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return create_plan(chabo, advertiser_account_id=session["account_id"], title=payload.title, creative_id=payload.creative_id)

    @api.get("/api/advertiser/plans/{plan_id}")
    def get_advertiser_plan(
        plan_id: str,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return get_plan(chabo, advertiser_account_id=session["account_id"], plan_id=plan_id)

    @api.post("/api/advertiser/plans/{plan_id}/items")
    def add_advertiser_plan_items(
        plan_id: str,
        payload: PlanAddItemsRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return add_plan_items(
            chabo,
            advertiser_account_id=session["account_id"],
            plan_id=plan_id,
            channel_ids=payload.channel_ids,
            slot_type=payload.slot_type,
            schedule_mode=payload.schedule_mode,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            frequency_per_day=payload.frequency_per_day,
            pin_enabled=payload.pin_enabled,
        )

    @api.post("/api/advertiser/plans/{plan_id}/submit")
    def submit_advertiser_plan(
        plan_id: str,
        payload: PlanSubmitRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("advertiser")),
    ) -> dict[str, Any]:
        return submit_plan(
            chabo,
            advertiser_account_id=session["account_id"],
            advertiser_telegram_user_id=session["telegram_user_id"],
            plan_id=plan_id,
            session_id=session["id"],
        )

    @api.get("/api/publisher/dashboard")
    def publisher_home(
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("publisher")),
    ) -> dict[str, Any]:
        return publisher_dashboard(chabo, session["account_id"])

    @api.get("/api/publisher/channels")
    def list_publisher_channels_api(
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("publisher")),
    ) -> dict[str, Any]:
        return publisher_channels(chabo, telegram_user_id=session["telegram_user_id"])

    @api.get("/api/publisher/earnings")
    def publisher_earnings(
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("publisher")),
    ) -> dict[str, Any]:
        telegram_user_id = session["telegram_user_id"]
        return {
            "summary": chabo.ledger.get_earnings_summary(telegram_user_id=telegram_user_id),
            "channels": chabo.ledger.list_channel_earnings(publisher_telegram_user_id=telegram_user_id),
        }

    @api.get("/api/publisher/channels/{channel_id}")
    def get_publisher_channel_api(
        channel_id: str,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("publisher")),
    ) -> dict[str, Any]:
        return chabo.channels.get_channel_view(channel_id, publisher_telegram_user_id=session["telegram_user_id"])

    @api.get("/api/publisher/channels/{channel_id}/deliveries")
    def list_publisher_channel_deliveries_api(
        channel_id: str,
        limit: int = 50,
        offset: int = 0,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("publisher")),
    ) -> dict[str, Any]:
        return publisher_channel_deliveries(
            chabo,
            telegram_user_id=session["telegram_user_id"],
            channel_id=channel_id,
            limit=limit,
            offset=offset,
        )

    @api.patch("/api/publisher/channels/{channel_id}/daily-limit")
    def update_publisher_daily_limit(
        channel_id: str,
        payload: DailyLimitRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("publisher")),
    ) -> dict[str, Any]:
        return chabo.channels.set_daily_ad_limit_for_publisher(
            publisher_telegram_user_id=session["telegram_user_id"],
            channel_id=channel_id,
            daily_ad_limit=payload.daily_ad_limit,
            actor_kind="human",
            session_id=session["id"],
        )

    @api.patch("/api/publisher/channels/{channel_id}/format-policy")
    def update_publisher_format_policy(
        channel_id: str,
        payload: FormatPolicyRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("publisher")),
    ) -> dict[str, Any]:
        return chabo.channels.set_format_policy_for_publisher(
            publisher_telegram_user_id=session["telegram_user_id"],
            channel_id=channel_id,
            format_type=payload.format_type,
            enabled=payload.enabled,
            owner_price_band=payload.owner_price_band,
            platform_promo_enabled=payload.platform_promo_enabled,
            custom_multiplier_bps=payload.custom_multiplier_bps,
            actor_kind="human",
            session_id=session["id"],
        )

    @api.patch("/api/publisher/channels/{channel_id}/rate")
    def update_publisher_rate(
        channel_id: str,
        payload: RateRequest,
        chabo: ChaboApp = Depends(get_chabo_app),
        session: dict[str, Any] = Depends(require_portal("publisher")),
    ) -> dict[str, Any]:
        return chabo.channels.set_rate_for_publisher(
            publisher_telegram_user_id=session["telegram_user_id"],
            channel_id=channel_id,
            slot_type=payload.slot_type,
            unit_price_cents=payload.unit_price_cents,
            actor_kind="human",
            session_id=session["id"],
        )


def run_api(
    *,
    settings: Settings | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    import uvicorn

    settings = settings or Settings.from_env()
    uvicorn.run(
        create_web_api(settings),
        host=host or settings.api_host,
        port=port or settings.api_port,
        log_level="info",
        proxy_headers=settings.api_proxy_headers,
        forwarded_allow_ips=settings.api_forwarded_allow_ips,
    )
