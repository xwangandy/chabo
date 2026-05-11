from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status

from ..app import ChaboApp, create_app
from ..config import Settings
from .auth import (
    ADMIN_LEVEL_ORDER,
    active_portals,
    admin_level,
    grant_portal,
    load_session,
    portal_statuses,
    sync_portal_access_from_activity,
)


DEV_BYPASS_PORTALS = ("admin", "advertiser", "publisher")


def get_chabo_app(request: Request) -> ChaboApp:
    return request.app.state.chabo


def current_session(
    request: Request,
    chabo: ChaboApp = Depends(get_chabo_app),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    token = request.cookies.get(chabo.settings.session_cookie_name)
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    session = load_session(chabo, token)
    if not session:
        if chabo.settings.dev_auth_bypass and not chabo.settings.is_production:
            return _dev_bypass_session(chabo)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    with chabo.db.transaction() as conn:
        activation = sync_portal_access_from_activity(conn, session["account_id"])
        session["portals"] = active_portals(conn, session["account_id"])
        session["portal_statuses"] = portal_statuses(conn, session["account_id"])
        session["activation"] = activation
        session["admin_level"] = admin_level(conn, session["account_id"])
    return session


def _dev_bypass_session(chabo: ChaboApp) -> dict[str, Any]:
    with chabo.db.transaction() as conn:
        account = chabo.ledger.accounts.get_or_create_by_telegram(
            conn,
            chabo.settings.dev_auth_telegram_user_id,
            "mixed",
            chabo.settings.dev_auth_display_name,
        )
        portals = active_portals(conn, account["id"])
        for portal in DEV_BYPASS_PORTALS:
            if portal not in portals or (portal == "admin" and admin_level(conn, account["id"]) != "super_admin"):
                grant_portal(
                    conn,
                    account_id=account["id"],
                    portal=portal,
                    status="active",
                    reason="dev_auth_bypass",
                    metadata={"admin_level": "super_admin"} if portal == "admin" else None,
                )
        portals = active_portals(conn, account["id"])
        statuses = portal_statuses(conn, account["id"])
        level = admin_level(conn, account["id"])
    return {
        "id": "dev_auth_bypass",
        "account_id": account["id"],
        "telegram_user_id": account["telegram_user_id"],
        "role": account["role"],
        "display_name": account["display_name"],
        "impersonator_account_id": None,
        "source": "dev_auth_bypass",
        "portals": portals,
        "portal_statuses": statuses,
        "admin_level": level,
        "activation": {
            "advertiser_successful_orders": 0,
            "advertiser_total_orders": 0,
            "publisher_owned_channels": 0,
            "publisher_produced_deliveries": 0,
            "promoted": [],
        },
    }


def require_portal(portal: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def dependency(session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        portals = session.get("portals", [])
        if "admin" not in portals and portal not in portals:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"{portal}_portal_required")
        return session

    return dependency


def require_admin(session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
    if "admin" not in session.get("portals", []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_portal_required")
    return session


def require_admin_level(required_level: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def dependency(session: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
        current_level = session.get("admin_level") or "viewer"
        if ADMIN_LEVEL_ORDER.get(current_level, 0) < ADMIN_LEVEL_ORDER[required_level]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"admin_{required_level}_required")
        return session

    return dependency


def make_chabo(settings: Settings | None = None) -> ChaboApp:
    return create_app(settings or Settings.from_env())
