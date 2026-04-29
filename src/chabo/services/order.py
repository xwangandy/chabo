from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..db import Database
from ..ids import new_id, new_ref_token
from ..money import bps_amount

from ._common import (
    ChaboError,
    InsufficientBalance,
    InvalidState,
    NotFound,
    _audit_payload_with_note,
    iso,
    parse_iso,
    row_to_dict,
    utcnow,
)
from .wallet import (
    AccountService,
    LedgerService,
    ToolCallLogService,
)
from .channel import (
    ChannelService,
    PricingService,
)


class MaterialService:
    """Independent ad material (creative) library.

    Stable boundary for Bot, CLI, Admin and future AI tool calls. All
    public methods validate ownership through advertiser_telegram_user_id.
    Format codes are internal: light_tail (文字插播), standard_card (标准插播),
    strong_post (定制插播); display layers translate to Chinese product names.
    """

    SUPPORTED_FORMATS = ("light_tail", "standard_card", "strong_post")
    LIGHT_SHORT_TEXT_MIN = 2
    LIGHT_SHORT_TEXT_MAX = 15
    LIBRARY_CAMPAIGN_NAME = "插播素材库"

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.tool_calls = ToolCallLogService(db, settings)

    def create_material(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        format_type: str,
        text: str,
        target_url: str,
        button_text: str = "查看详情",
        category: str = "general",
        light_short_text: str | None = None,
        display_name: str | None = None,
        actor_kind: str = "human",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {
            "format_type": format_type,
            "target_url": target_url,
            "category": category,
            "text_preview": (text or "")[:60],
            "has_light_short_text": bool(light_short_text),
        }
        try:
            format_type = self._normalize_format(format_type)
            text, target_url, button_text, category = self._normalize_text_fields(
                text, target_url, button_text, category
            )
            if format_type == "light_tail":
                short = (light_short_text or "").strip()
                if len(short) < self.LIGHT_SHORT_TEXT_MIN:
                    raise InvalidState(
                        f"文字插播短入口至少 {self.LIGHT_SHORT_TEXT_MIN} 个字"
                    )
                if len(short) > self.LIGHT_SHORT_TEXT_MAX:
                    raise InvalidState(
                        f"文字插播短入口最多 {self.LIGHT_SHORT_TEXT_MAX} 个字"
                    )
                light_short_text = short
            else:
                light_short_text = None

            with self.db.transaction() as conn:
                advertiser = self.accounts.get_or_create_by_telegram(
                    conn,
                    advertiser_telegram_user_id,
                    "advertiser",
                    display_name=display_name,
                )
                material_id = self._insert_material(
                    conn,
                    advertiser_account_id=advertiser["id"],
                    format_type=format_type,
                    text=text,
                    target_url=target_url,
                    button_text=button_text,
                    category=category,
                    light_short_text=light_short_text,
                )
                material = self._fetch_material(conn, material_id)
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="create_material",
                actor_telegram_user_id=advertiser_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="create_material",
            actor_telegram_user_id=advertiser_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"created {material['id']}",
        )
        return material

    def list_materials(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        format_type: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            advertiser = conn.execute(
                "SELECT id FROM accounts WHERE telegram_user_id = ?",
                (str(advertiser_telegram_user_id),),
            ).fetchone()
            if not advertiser:
                return []
            sql = "SELECT * FROM creatives WHERE advertiser_account_id = ?"
            params: list[Any] = [advertiser["id"]]
            if format_type is not None:
                sql += " AND format_type = ?"
                params.append(self._normalize_format(format_type))
            if not include_archived:
                sql += " AND archived_at IS NULL"
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def get_material(
        self,
        material_id: str,
        *,
        advertiser_telegram_user_id: str | int | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM creatives WHERE id = ?",
                (material_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"广告素材不存在：{material_id}")
            material = dict(row)
            if advertiser_telegram_user_id is not None:
                advertiser = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(advertiser_telegram_user_id),),
                ).fetchone()
                if not advertiser or material["advertiser_account_id"] != advertiser["id"]:
                    raise NotFound(f"广告素材不存在：{material_id}")
            return material

    def archive_material(
        self,
        material_id: str,
        *,
        advertiser_telegram_user_id: str | int,
        actor_kind: str = "human",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {"material_id": material_id}
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM creatives WHERE id = ?",
                    (material_id,),
                ).fetchone()
                if not row:
                    raise NotFound(f"广告素材不存在：{material_id}")
                advertiser = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(advertiser_telegram_user_id),),
                ).fetchone()
                if not advertiser or row["advertiser_account_id"] != advertiser["id"]:
                    raise NotFound(f"广告素材不存在：{material_id}")
                if row["archived_at"]:
                    material = dict(row)
                    already_archived = True
                else:
                    conn.execute(
                        "UPDATE creatives SET archived_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (iso(), material_id),
                    )
                    material = self._fetch_material(conn, material_id)
                    already_archived = False
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="archive_material",
                actor_telegram_user_id=advertiser_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="archive_material",
            actor_telegram_user_id=advertiser_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=("already archived" if already_archived else f"archived {material_id}"),
        )
        return material

    # ------ helpers reusable from OrderService inside an open transaction ------

    def insert_material_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        advertiser_account_id: str,
        format_type: str,
        text: str,
        target_url: str,
        button_text: str = "查看详情",
        category: str = "general",
        light_short_text: str | None = None,
        campaign_id: str | None = None,
    ) -> str:
        format_type = self._normalize_format(format_type)
        text, target_url, button_text, category = self._normalize_text_fields(
            text, target_url, button_text, category
        )
        if format_type == "light_tail" and light_short_text:
            light_short_text = light_short_text.strip() or None
        else:
            light_short_text = None
        return self._insert_material(
            conn,
            advertiser_account_id=advertiser_account_id,
            format_type=format_type,
            text=text,
            target_url=target_url,
            button_text=button_text,
            category=category,
            light_short_text=light_short_text,
            campaign_id=campaign_id,
        )

    def list_materials_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        advertiser_account_id: str,
        format_type: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM creatives WHERE advertiser_account_id = ?"
        params: list[Any] = [advertiser_account_id]
        if format_type is not None:
            sql += " AND format_type = ?"
            params.append(self._normalize_format(format_type))
        if not include_archived:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def archive_material_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        material_id: str,
        advertiser_account_id: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM creatives WHERE id = ?", (material_id,)
        ).fetchone()
        if not row or row["advertiser_account_id"] != advertiser_account_id:
            raise NotFound(f"广告素材不存在：{material_id}")
        if row["archived_at"]:
            return dict(row)
        conn.execute(
            "UPDATE creatives SET archived_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (iso(), material_id),
        )
        return self._fetch_material(conn, material_id)

    def ensure_library_campaign(
        self, conn: sqlite3.Connection, advertiser_account_id: str
    ) -> str:
        row = conn.execute(
            "SELECT id FROM campaigns WHERE advertiser_account_id = ? AND name = ? LIMIT 1",
            (advertiser_account_id, self.LIBRARY_CAMPAIGN_NAME),
        ).fetchone()
        if row:
            return row["id"]
        campaign_id = new_id("camp")
        conn.execute(
            "INSERT INTO campaigns (id, advertiser_account_id, name) VALUES (?, ?, ?)",
            (campaign_id, advertiser_account_id, self.LIBRARY_CAMPAIGN_NAME),
        )
        return campaign_id

    # ----------------------------- internals -----------------------------

    def _insert_material(
        self,
        conn: sqlite3.Connection,
        *,
        advertiser_account_id: str,
        format_type: str,
        text: str,
        target_url: str,
        button_text: str,
        category: str,
        light_short_text: str | None,
        campaign_id: str | None = None,
    ) -> str:
        if not text:
            raise InvalidState("广告素材文案不能为空")
        if not target_url:
            raise InvalidState("广告素材必须包含目标链接")
        if campaign_id is None:
            campaign_id = self.ensure_library_campaign(conn, advertiser_account_id)
        material_id = new_id("cre")
        content_hash = hashlib.sha256(
            f"{format_type}|{light_short_text or ''}|{text}|{target_url}|{button_text}".encode("utf-8")
        ).hexdigest()
        conn.execute(
            """
            INSERT INTO creatives (
                id, campaign_id, advertiser_account_id, format_type,
                text, target_url, button_text, category, light_short_text,
                status, content_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)
            """,
            (
                material_id,
                campaign_id,
                advertiser_account_id,
                format_type,
                text,
                target_url,
                button_text,
                category,
                light_short_text,
                content_hash,
            ),
        )
        return material_id

    def _fetch_material(self, conn: sqlite3.Connection, material_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM creatives WHERE id = ?", (material_id,)).fetchone()
        return dict(row)

    def _normalize_format(self, format_type: str) -> str:
        if format_type not in self.SUPPORTED_FORMATS:
            raise InvalidState(
                "不支持的素材形态："
                f"{format_type}（仅支持 light_tail / standard_card / strong_post）"
            )
        return format_type

    def _normalize_text_fields(
        self, text: str, target_url: str, button_text: str, category: str
    ) -> tuple[str, str, str, str]:
        text = (text or "").strip()
        target_url = (target_url or "").strip()
        button_text = (button_text or "查看详情").strip() or "查看详情"
        category = (category or "general").strip() or "general"
        return text, target_url, button_text, category


class DisputeService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)

    def open_dispute(
        self,
        *,
        opened_by_telegram_user_id: str | int,
        delivery_id: str,
        reason: str,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            opener = self.accounts.get_or_create_by_telegram(conn, opened_by_telegram_user_id, "advertiser")
            delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            if not delivery:
                raise NotFound(f"delivery not found: {delivery_id}")
            dispute_id = new_id("disp")
            conn.execute(
                """
                INSERT INTO disputes (id, order_id, delivery_id, opened_by_account_id, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (dispute_id, delivery["order_id"], delivery_id, opener["id"], reason),
            )
            conn.execute(
                "UPDATE deliveries SET status = 'disputed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (delivery_id,),
            )
            conn.execute(
                """
                INSERT INTO evidence_snapshots (id, order_id, delivery_id, snapshot_type, payload_json)
                VALUES (?, ?, ?, 'dispute_opened', ?)
                """,
                (
                    new_id("ev"),
                    delivery["order_id"],
                    delivery_id,
                    json.dumps({"reason": reason, "opened_by": opener["id"]}, ensure_ascii=False),
                ),
            )
            return dict(conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone())

    def list_disputes(self, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE d.status = ?"
            params.append(status)
        params.append(limit)
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT d.*, c.title AS channel_title, del.message_id, del.status AS delivery_status
                FROM disputes d
                JOIN deliveries del ON del.id = d.delivery_id
                JOIN channels c ON c.id = del.channel_id
                {where}
                ORDER BY d.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def resolve_dispute(
        self,
        *,
        dispute_id: str,
        resolution: str,
        actor_account_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            dispute = conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
            if not dispute:
                raise NotFound(f"dispute not found: {dispute_id}")
            if dispute["status"] != "open":
                raise InvalidState(f"争议状态不能裁决: {dispute['status']}")
            conn.execute(
                """
                UPDATE disputes
                SET status = 'resolved', resolution = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (resolution, dispute_id),
            )
            if dispute["delivery_id"]:
                conn.execute(
                    "UPDATE deliveries SET status = 'sent', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'disputed'",
                    (dispute["delivery_id"],),
                )
            payload = _audit_payload_with_note({"resolution": resolution}, note)
            conn.execute(
                """
                INSERT INTO audit_logs (id, actor_account_id, action, entity_type, entity_id, payload_json)
                VALUES (?, ?, 'dispute_resolved', 'dispute', ?, ?)
                """,
                (new_id("aud"), actor_account_id, dispute_id, json.dumps(payload, ensure_ascii=False)),
            )
            return dict(conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone())


class OrderService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings)
        self.ledger = LedgerService(db, settings)
        self.materials = MaterialService(db, settings)
        self.tool_calls = ToolCallLogService(db, settings)

    def create_order(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        channel_token: str,
        slot_type: str,
        budget_cents: int,
        text: str | None = None,
        target_url: str | None = None,
        button_text: str = "查看详情",
        category: str = "general",
        light_short_text: str | None = None,
        material_id: str | None = None,
        scheduled_at: datetime | None = None,
        end_at: datetime | None = None,
        frequency_per_day: int = 1,
        campaign_name: str = "插播广告",
        unit_price_override_cents: int | None = None,
        price_offer_id: str | None = None,
        actor_kind: str = "human",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {
            "channel_token": channel_token,
            "slot_type": slot_type,
            "budget_cents": budget_cents,
            "material_id": material_id,
            "has_inline_text": bool(text),
            "has_end_at": end_at is not None,
            "frequency_per_day": frequency_per_day,
            "price_offer_id": price_offer_id,
        }
        try:
            order = self._create_order_impl(
                advertiser_telegram_user_id=advertiser_telegram_user_id,
                channel_token=channel_token,
                slot_type=slot_type,
                budget_cents=budget_cents,
                text=text,
                target_url=target_url,
                button_text=button_text,
                category=category,
                light_short_text=light_short_text,
                material_id=material_id,
                scheduled_at=scheduled_at,
                end_at=end_at,
                frequency_per_day=frequency_per_day,
                campaign_name=campaign_name,
                unit_price_override_cents=unit_price_override_cents,
                price_offer_id=price_offer_id,
            )
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="create_order",
                actor_telegram_user_id=advertiser_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="create_order",
            actor_telegram_user_id=advertiser_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"created {order['id']}",
        )
        return order

    def _create_order_impl(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        channel_token: str,
        slot_type: str,
        budget_cents: int,
        text: str | None,
        target_url: str | None,
        button_text: str,
        category: str,
        light_short_text: str | None,
        material_id: str | None,
        scheduled_at: datetime | None,
        end_at: datetime | None,
        frequency_per_day: int,
        campaign_name: str,
        unit_price_override_cents: int | None,
        price_offer_id: str | None,
    ) -> dict[str, Any]:
        scheduled_at = scheduled_at or utcnow()
        slot_type = self.channels.normalize_slot_type(slot_type)
        if material_id is None and not (text and target_url):
            raise InvalidState("创建订单需要提供 material_id 或者 text+target_url")
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            channel = self.channels.get_by_token(conn, channel_token)
            if not channel:
                raise NotFound("频道插播入口不存在或已失效")
            rate = self.channels.get_rate(conn, channel["id"], slot_type)
            policy = conn.execute(
                """
                SELECT * FROM channel_ad_format_policies
                WHERE channel_id = ? AND format_type = ?
                """,
                (channel["id"], slot_type),
            ).fetchone()
            if policy and not policy["enabled"]:
                raise InvalidState("该频道主当前未开启这种插播广告形态")
            from .billing import SubscriptionService
            effective_service_fee_bps = 0 if SubscriptionService.has_active_subscription(conn, channel["id"]) else self.settings.default_service_fee_bps
            if policy and policy["platform_promo_enabled"]:
                effective_service_fee_bps = 0
            conn.execute(
                """
                UPDATE channel_configs
                SET service_fee_bps = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ?
                """,
                (effective_service_fee_bps, channel["id"]),
            )
            unit_price_cents = unit_price_override_cents or rate["unit_price_cents"]
            if budget_cents < unit_price_cents:
                raise InvalidState("插播预算低于该广告位单次刊例价")
            campaign_id = new_id("camp")
            order_id = new_id("ord")
            conn.execute(
                """
                INSERT INTO campaigns (id, advertiser_account_id, name)
                VALUES (?, ?, ?)
                """,
                (campaign_id, advertiser["id"], campaign_name),
            )
            if material_id is not None:
                material = conn.execute(
                    "SELECT * FROM creatives WHERE id = ?", (material_id,)
                ).fetchone()
                if not material or material["advertiser_account_id"] != advertiser["id"]:
                    raise NotFound(f"广告素材不存在：{material_id}")
                if material["archived_at"]:
                    raise InvalidState("广告素材已归档，请先恢复或选择其他素材")
                creative_id = material["id"]
                creative_snapshot = {
                    "text": material["text"],
                    "target_url": material["target_url"],
                    "button_text": material["button_text"],
                    "format_type": material["format_type"],
                    "light_short_text": material["light_short_text"],
                    "material_id": material["id"],
                }
            else:
                material_format = (
                    slot_type
                    if slot_type in MaterialService.SUPPORTED_FORMATS
                    else "standard_card"
                )
                creative_id = self.materials.insert_material_in_conn(
                    conn,
                    advertiser_account_id=advertiser["id"],
                    format_type=material_format,
                    text=text or "",
                    target_url=target_url or "",
                    button_text=button_text,
                    category=category,
                    light_short_text=light_short_text,
                    campaign_id=campaign_id,
                )
                creative_snapshot = {
                    "text": text,
                    "target_url": target_url,
                    "button_text": button_text,
                    "format_type": material_format,
                    "light_short_text": light_short_text,
                    "material_id": creative_id,
                }
            conn.execute(
                """
                INSERT INTO ad_orders (
                    id, campaign_id, creative_id, advertiser_account_id, channel_id, slot_id,
                    budget_cents, reserved_cents, unit_price_cents, start_at, end_at,
                    scheduled_at, frequency_per_day, price_offer_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    campaign_id,
                    creative_id,
                    advertiser["id"],
                    channel["id"],
                    rate["slot_id"],
                    budget_cents,
                    budget_cents,
                    unit_price_cents,
                    iso(scheduled_at),
                    iso(end_at) if end_at else None,
                    iso(scheduled_at),
                    frequency_per_day,
                    price_offer_id,
                ),
            )
            self.ledger.reserve_budget(conn, advertiser["id"], order_id, budget_cents, rate["currency"])
            self._snapshot(conn, order_id, None, "creative", creative_snapshot)
            self._snapshot(conn, order_id, None, "rate_card", {**rate, "accepted_unit_price_cents": unit_price_cents, "price_offer_id": price_offer_id})
            self._snapshot(conn, order_id, None, "channel", channel)
            return self.get_order(conn, order_id)

    def approve_order_for_operator(
        self,
        *,
        order_id: str,
        operator_telegram_user_id: str | int,
        note: str | None = None,
        actor_kind: str = "admin",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {"order_id": order_id, "has_note": bool((note or "").strip())}
        try:
            order = self.approve_order(order_id, self._operator_account_id(operator_telegram_user_id), note=note)
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="approve_order_for_operator",
                actor_telegram_user_id=operator_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="approve_order_for_operator",
            actor_telegram_user_id=operator_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"approved {order_id}",
        )
        return order

    def reject_order_for_operator(
        self,
        *,
        order_id: str,
        reason: str,
        operator_telegram_user_id: str | int,
        note: str | None = None,
        actor_kind: str = "admin",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {
            "order_id": order_id,
            "reason_preview": (reason or "")[:80],
            "has_note": bool((note or "").strip()),
        }
        try:
            order = self.reject_order(order_id, reason, self._operator_account_id(operator_telegram_user_id), note=note)
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="reject_order_for_operator",
                actor_telegram_user_id=operator_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        self.tool_calls.log_success(
            tool_name="reject_order_for_operator",
            actor_telegram_user_id=operator_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"rejected {order_id}",
        )
        return order

    def refund_delivery_for_operator(
        self,
        *,
        delivery_id: str,
        reason: str,
        operator_telegram_user_id: str | int,
        amount_cents: int | None = None,
        note: str | None = None,
        actor_kind: str = "admin",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        audit_args = {
            "delivery_id": delivery_id,
            "amount_cents": amount_cents,
            "reason_preview": (reason or "")[:80],
            "has_note": bool((note or "").strip()),
        }
        try:
            actor = self._operator_account_id(operator_telegram_user_id)
            if amount_cents is None:
                delivery = self.refund_delivery(delivery_id, reason, actor, note=note)
            else:
                delivery = self.refund_delivery_partial(delivery_id, amount_cents, reason, actor, note=note)
        except ChaboError as exc:
            self.tool_calls.log_failure(
                tool_name="refund_delivery_for_operator",
                actor_telegram_user_id=operator_telegram_user_id,
                actor_kind=actor_kind,
                session_id=session_id,
                arguments=audit_args,
                error=exc,
            )
            raise
        kind = "full" if amount_cents is None else f"partial {amount_cents}"
        self.tool_calls.log_success(
            tool_name="refund_delivery_for_operator",
            actor_telegram_user_id=operator_telegram_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            arguments=audit_args,
            result_summary=f"{kind} refund on {delivery_id}",
        )
        return delivery

    def _operator_account_id(self, telegram_user_id: str | int) -> str:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM accounts WHERE telegram_user_id = ?",
                (str(telegram_user_id),),
            ).fetchone()
            if not row:
                raise NotFound(f"操作员账号不存在：{telegram_user_id}")
            return row["id"]

    def approve_order(
        self,
        order_id: str,
        actor_account_id: str | None = None,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            order = self.get_order(conn, order_id)
            if order["status"] not in {"pending_review", "paused"}:
                raise InvalidState(f"订单状态不能审核通过: {order['status']}")
            conn.execute("UPDATE creatives SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order["creative_id"],))
            conn.execute(
                """
                UPDATE ad_orders
                SET status = 'approved', approved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order_id,),
            )
            self._ensure_delivery(conn, order_id, order["scheduled_at"])
            payload = _audit_payload_with_note({}, note)
            self._audit(conn, actor_account_id, "order_approved", "ad_order", order_id, payload)
            return self.get_order(conn, order_id)

    def reject_order(
        self,
        order_id: str,
        reason: str,
        actor_account_id: str | None = None,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            order = self.get_order(conn, order_id)
            if order["spent_cents"] > 0:
                raise InvalidState("已有成功扣费的订单不能直接拒绝，请走退款或争议裁决")
            if order["status"] not in {"pending_review", "approved", "paused"}:
                raise InvalidState(f"订单状态不能拒绝: {order['status']}")
            if order["reserved_cents"]:
                self.ledger.release_reserved(
                    conn,
                    order["advertiser_account_id"],
                    order_id,
                    order["reserved_cents"],
                    reason=f"插播订单被拒绝：{reason}",
                    currency=order["currency"],
                )
            conn.execute("UPDATE creatives SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order["creative_id"],))
            conn.execute("UPDATE deliveries SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE order_id = ? AND status = 'scheduled'", (order_id,))
            conn.execute(
                """
                UPDATE ad_orders
                SET status = 'rejected', reserved_cents = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order_id,),
            )
            self._snapshot(conn, order_id, None, "order_rejected", {"reason": reason})
            payload = _audit_payload_with_note({"reason": reason}, note)
            self._audit(conn, actor_account_id, "order_rejected", "ad_order", order_id, payload)
            return self.get_order(conn, order_id)

    def refund_delivery(
        self,
        delivery_id: str,
        reason: str,
        actor_account_id: str | None = None,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            delivery = self._refundable_delivery(conn, delivery_id)
            refundable_cents = delivery["charge_cents"] - delivery["refunded_cents"]
            return self._refund_delivery_locked(conn, delivery, reason, refundable_cents, actor_account_id, note=note)

    def refund_delivery_partial(
        self,
        delivery_id: str,
        amount_cents: int,
        reason: str,
        actor_account_id: str | None = None,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        if amount_cents <= 0:
            raise InvalidState("退款金额必须大于 0")
        with self.db.transaction() as conn:
            delivery = self._refundable_delivery(conn, delivery_id)
            refundable_cents = delivery["charge_cents"] - delivery["refunded_cents"]
            if amount_cents > refundable_cents:
                raise InvalidState("退款金额超过该投放可退款余额")
            return self._refund_delivery_locked(conn, delivery, reason, amount_cents, actor_account_id, note=note)

    def _refundable_delivery(self, conn: sqlite3.Connection, delivery_id: str) -> sqlite3.Row:
        delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if not delivery:
            raise NotFound(f"delivery not found: {delivery_id}")
        if delivery["status"] not in {"sent", "disputed"}:
            raise InvalidState(f"当前投放状态不能退款: {delivery['status']}")
        if delivery["charge_cents"] <= 0:
            raise InvalidState("该投放没有成功扣费，不能退款")
        if delivery["charge_cents"] - delivery["refunded_cents"] <= 0:
            raise InvalidState("该投放已无可退款金额")
        return delivery

    def _refund_delivery_locked(
        self,
        conn: sqlite3.Connection,
        delivery: sqlite3.Row,
        reason: str,
        amount_cents: int,
        actor_account_id: str | None,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        order = self.get_order(conn, delivery["order_id"])
        channel = conn.execute("SELECT * FROM channels WHERE id = ?", (delivery["channel_id"],)).fetchone()
        if not channel:
            raise NotFound("channel not found")
        refundable_cents = delivery["charge_cents"] - delivery["refunded_cents"]
        if amount_cents <= 0 or amount_cents > refundable_cents:
            raise InvalidState("退款金额不合法")
        publisher_net_cents, platform_fee_cents = self._split_refund_amount(delivery, amount_cents, refundable_cents)
        full_refund = amount_cents == refundable_cents
        self.ledger.refund_delivery(
            conn,
            advertiser_account_id=order["advertiser_account_id"],
            publisher_account_id=channel["owner_account_id"],
            order_id=order["id"],
            delivery_id=delivery["id"],
            gross_cents=amount_cents,
            publisher_net_cents=publisher_net_cents,
            platform_fee_cents=platform_fee_cents,
            reason=reason,
            currency=order["currency"],
        )
        if full_refund and order["reserved_cents"]:
            self.ledger.release_reserved(
                conn,
                order["advertiser_account_id"],
                order["id"],
                order["reserved_cents"],
                reason="插播退款后停止订单，释放剩余预算",
                currency=order["currency"],
            )
        order_status_sql = "status = 'refunded', reserved_cents = 0," if full_refund else ""
        conn.execute(
            f"""
            UPDATE ad_orders
            SET {order_status_sql}
                spent_cents = MAX(spent_cents - ?, 0),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, order["id"]),
        )
        delivery_status_sql = "status = 'refunded'," if full_refund else ""
        conn.execute(
            f"""
            UPDATE deliveries
            SET {delivery_status_sql}
                refunded_cents = refunded_cents + ?,
                publisher_reversed_cents = publisher_reversed_cents + ?,
                platform_fee_reversed_cents = platform_fee_reversed_cents + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (amount_cents, publisher_net_cents, platform_fee_cents, delivery["id"]),
        )
        if full_refund:
            conn.execute(
                """
                UPDATE disputes
                SET status = 'resolved',
                    resolution = ?,
                    resolved_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ? AND status = 'open'
                """,
                (f"已退款：{reason}", delivery["id"]),
            )
        snapshot_type = "delivery_refunded" if full_refund else "delivery_partially_refunded"
        self._snapshot(
            conn,
            order["id"],
            delivery["id"],
            snapshot_type,
            {
                "reason": reason,
                "refund_cents": amount_cents,
                "publisher_net_reversed_cents": publisher_net_cents,
                "platform_fee_reversed_cents": platform_fee_cents,
                "full_refund": full_refund,
            },
        )
        self._audit(
            conn,
            actor_account_id,
            "delivery_refunded" if full_refund else "delivery_partially_refunded",
            "delivery",
            delivery["id"],
            _audit_payload_with_note(
                {"reason": reason, "refund_cents": amount_cents, "full_refund": full_refund},
                note,
            ),
        )
        return {
            "order": self.get_order(conn, order["id"]),
            "delivery": dict(conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery["id"],)).fetchone()),
        }

    def _split_refund_amount(
        self,
        delivery: sqlite3.Row,
        amount_cents: int,
        refundable_cents: int,
    ) -> tuple[int, int]:
        remaining_net = delivery["publisher_net_cents"] - delivery["publisher_reversed_cents"]
        remaining_fee = delivery["platform_fee_cents"] - delivery["platform_fee_reversed_cents"]
        if amount_cents == refundable_cents:
            return remaining_net, remaining_fee
        publisher_net_cents = min(remaining_net, (amount_cents * remaining_net) // refundable_cents)
        platform_fee_cents = amount_cents - publisher_net_cents
        if platform_fee_cents > remaining_fee:
            platform_fee_cents = remaining_fee
            publisher_net_cents = amount_cents - platform_fee_cents
        if publisher_net_cents > remaining_net:
            publisher_net_cents = remaining_net
            platform_fee_cents = amount_cents - publisher_net_cents
        return publisher_net_cents, platform_fee_cents

    def pause_and_release(self, conn: sqlite3.Connection, order_id: str, reason: str) -> None:
        order = self.get_order(conn, order_id)
        remaining = order["reserved_cents"]
        if remaining:
            self.ledger.release_reserved(
                conn,
                order["advertiser_account_id"],
                order_id,
                remaining,
                reason=reason,
                currency=order["currency"],
            )
        conn.execute(
            """
            UPDATE ad_orders
            SET status = 'paused', reserved_cents = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (order_id,),
        )

    def get_order(self, conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM ad_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            raise NotFound(f"order not found: {order_id}")
        return dict(row)

    def list_orders(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            account = conn.execute(
                "SELECT id FROM accounts WHERE telegram_user_id = ?",
                (str(advertiser_telegram_user_id),),
            ).fetchone()
            if not account:
                return []
            sql = (
                "SELECT o.*, c.title AS channel_title, c.ref_token AS channel_ref_token, "
                "       cr.format_type AS creative_format, cr.text AS creative_text "
                "FROM ad_orders o "
                "LEFT JOIN channels c ON c.id = o.channel_id "
                "LEFT JOIN creatives cr ON cr.id = o.creative_id "
                "WHERE o.advertiser_account_id = ?"
            )
            params: list[Any] = [account["id"]]
            if status is not None:
                sql += " AND o.status = ?"
                params.append(status)
            sql += " ORDER BY o.created_at DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def get_order_view(
        self,
        order_id: str,
        *,
        advertiser_telegram_user_id: str | int | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            order = conn.execute(
                "SELECT * FROM ad_orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise NotFound(f"订单不存在：{order_id}")
            order_dict = dict(order)
            if advertiser_telegram_user_id is not None:
                account = conn.execute(
                    "SELECT id FROM accounts WHERE telegram_user_id = ?",
                    (str(advertiser_telegram_user_id),),
                ).fetchone()
                if not account or order_dict["advertiser_account_id"] != account["id"]:
                    raise NotFound(f"订单不存在：{order_id}")
            channel = conn.execute(
                "SELECT id, title, ref_token, username FROM channels WHERE id = ?",
                (order_dict["channel_id"],),
            ).fetchone()
            creative = conn.execute(
                "SELECT id, format_type, text, target_url, button_text, light_short_text "
                "FROM creatives WHERE id = ?",
                (order_dict["creative_id"],),
            ).fetchone()
            slot = conn.execute(
                "SELECT slot_type FROM ad_slots WHERE id = ?", (order_dict["slot_id"],)
            ).fetchone()
            deliveries = conn.execute(
                "SELECT id, status, scheduled_at, sent_at, charge_cents, message_id "
                "FROM deliveries WHERE order_id = ? ORDER BY scheduled_at ASC",
                (order_id,),
            ).fetchall()
            return {
                **order_dict,
                "channel": dict(channel) if channel else None,
                "creative": dict(creative) if creative else None,
                "slot_type": slot["slot_type"] if slot else None,
                "deliveries": [dict(row) for row in deliveries],
            }

    def _ensure_delivery(self, conn: sqlite3.Connection, order_id: str, scheduled_at: str) -> str:
        existing = conn.execute(
            "SELECT id FROM deliveries WHERE order_id = ? AND status = 'scheduled' ORDER BY scheduled_at LIMIT 1",
            (order_id,),
        ).fetchone()
        if existing:
            return existing["id"]
        order = self.get_order(conn, order_id)
        delivery_id = new_id("del")
        conn.execute(
            """
            INSERT INTO deliveries (id, order_id, channel_id, creative_id, scheduled_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (delivery_id, order_id, order["channel_id"], order["creative_id"], scheduled_at),
        )
        return delivery_id

    def maybe_schedule_next(self, conn: sqlite3.Connection, order_id: str) -> None:
        order = self.get_order(conn, order_id)
        slot = conn.execute("SELECT * FROM ad_slots WHERE id = ?", (order["slot_id"],)).fetchone()
        remaining = order["reserved_cents"]
        if remaining < order["unit_price_cents"]:
            if remaining > 0:
                self.ledger.release_reserved(
                    conn,
                    order["advertiser_account_id"],
                    order_id,
                    remaining,
                    reason="插播预算不足以继续下一次发布，释放余款",
                    currency=order["currency"],
                )
            conn.execute(
                """
                UPDATE ad_orders
                SET status = 'budget_exhausted', reserved_cents = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order_id,),
            )
            return
        if slot["slot_type"] != "loop_daily" and not order["end_at"]:
            return
        next_at = parse_iso(order["scheduled_at"]) + timedelta(days=1)
        if order["end_at"] and next_at > parse_iso(order["end_at"]):
            self.ledger.release_reserved(
                conn,
                order["advertiser_account_id"],
                order_id,
                remaining,
                reason="插播周期结束，释放剩余预算",
                currency=order["currency"],
            )
            conn.execute(
                "UPDATE ad_orders SET status = 'done', reserved_cents = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (order_id,),
            )
            return
        conn.execute(
            "UPDATE ad_orders SET status = 'running', scheduled_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (iso(next_at), order_id),
        )
        self._ensure_delivery(conn, order_id, iso(next_at))

    def mark_low_budget_notified(self, conn: sqlite3.Connection, order_id: str) -> None:
        conn.execute(
            "UPDATE ad_orders SET low_budget_notified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (order_id,),
        )

    def _snapshot(
        self,
        conn: sqlite3.Connection,
        order_id: str | None,
        delivery_id: str | None,
        snapshot_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO evidence_snapshots (id, order_id, delivery_id, snapshot_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id("ev"), order_id, delivery_id, snapshot_type, json.dumps(payload, ensure_ascii=False, default=str)),
        )

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor_account_id: str | None,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_logs (id, actor_account_id, action, entity_type, entity_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("aud"), actor_account_id, action, entity_type, entity_id, json.dumps(payload, ensure_ascii=False)),
        )


class PriceOfferService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountService(db, settings)
        self.channels = ChannelService(db, settings)
        self.pricing = PricingService(db, settings)
        self.ledger = LedgerService(db, settings)
        self.materials = MaterialService(db, settings)

    def create_offer(
        self,
        *,
        advertiser_telegram_user_id: str | int,
        channel_id: str,
        slot_type: str,
        offered_price_cents: int,
        creative_text: str,
        target_url: str,
        budget_cents: int | None = None,
        button_text: str = "查看详情",
        category: str = "general",
        scheduled_at: datetime | None = None,
        end_at: datetime | None = None,
        frequency_per_day: int = 1,
        message: str | None = None,
    ) -> dict[str, Any]:
        slot_type = self.channels.normalize_slot_type(slot_type)
        scheduled_at = scheduled_at or utcnow()
        budget_cents = budget_cents or offered_price_cents
        if budget_cents < offered_price_cents:
            raise InvalidState("砍价预算不能低于报价单次价格")
        if not creative_text.strip():
            raise InvalidState("砍价报价必须包含插播广告文案")
        if not target_url.strip():
            raise InvalidState("砍价报价必须包含广告目标链接")
        quote = self.pricing.quote_channel(channel_id, slot_type)
        with self.db.transaction() as conn:
            advertiser = self.accounts.get_or_create_by_telegram(conn, advertiser_telegram_user_id, "advertiser")
            offer_id = new_id("offer")
            conn.execute(
                """
                INSERT INTO price_offers (
                    id, advertiser_account_id, channel_id, slot_type,
                    offered_price_cents, list_price_cents, budget_cents,
                    creative_text, target_url, button_text, category,
                    scheduled_at, end_at, frequency_per_day, message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    offer_id,
                    advertiser["id"],
                    channel_id,
                    slot_type,
                    offered_price_cents,
                    quote["list_price_cents"],
                    budget_cents,
                    creative_text,
                    target_url,
                    button_text,
                    category,
                    iso(scheduled_at),
                    iso(end_at) if end_at else None,
                    frequency_per_day,
                    message,
                ),
            )
            return dict(conn.execute("SELECT * FROM price_offers WHERE id = ?", (offer_id,)).fetchone())

    def respond_offer(self, offer_id: str, *, accepted: bool) -> dict[str, Any]:
        with self.db.transaction() as conn:
            offer = conn.execute("SELECT * FROM price_offers WHERE id = ?", (offer_id,)).fetchone()
            if not offer:
                raise NotFound(f"price offer not found: {offer_id}")
            if offer["status"] != "pending":
                raise InvalidState("该砍价报价已经处理过")
            order_id = None
            if accepted:
                order_id = self._create_order_from_offer(conn, offer)
            conn.execute(
                """
                UPDATE price_offers
                SET status = ?, accepted_order_id = ?, responded_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                ("accepted" if accepted else "rejected", order_id, offer_id),
            )
            return dict(conn.execute("SELECT * FROM price_offers WHERE id = ?", (offer_id,)).fetchone())

    def _create_order_from_offer(self, conn: sqlite3.Connection, offer: sqlite3.Row) -> str:
        channel = self.channels.get_channel(conn, offer["channel_id"])
        rate = self.channels.get_rate(conn, offer["channel_id"], offer["slot_type"])
        policy = conn.execute(
            """
            SELECT * FROM channel_ad_format_policies
            WHERE channel_id = ? AND format_type = ?
            """,
            (offer["channel_id"], offer["slot_type"]),
        ).fetchone()
        if policy and not policy["enabled"]:
            raise InvalidState("该频道主当前未开启这种插播广告形态")
        from .billing import SubscriptionService
        effective_service_fee_bps = 0 if SubscriptionService.has_active_subscription(conn, channel["id"]) else self.settings.default_service_fee_bps
        if policy and policy["platform_promo_enabled"]:
            effective_service_fee_bps = 0
        conn.execute(
            """
            UPDATE channel_configs
            SET service_fee_bps = ?, updated_at = CURRENT_TIMESTAMP
            WHERE channel_id = ?
            """,
            (effective_service_fee_bps, channel["id"]),
        )
        budget_cents = offer["budget_cents"] or offer["offered_price_cents"]
        if budget_cents < offer["offered_price_cents"]:
            raise InvalidState("砍价预算不能低于报价单次价格")

        advertiser = self.accounts.get(conn, offer["advertiser_account_id"])
        if advertiser["available_balance_cents"] < budget_cents:
            raise InsufficientBalance("广告主插播余额不足，接受砍价后无法冻结预算")

        campaign_id = new_id("camp")
        order_id = new_id("ord")
        scheduled_at = offer["scheduled_at"] or iso()
        conn.execute(
            """
            INSERT INTO campaigns (id, advertiser_account_id, name)
            VALUES (?, ?, ?)
            """,
            (campaign_id, offer["advertiser_account_id"], "砍价成交插播广告"),
        )
        material_format = (
            offer["slot_type"]
            if offer["slot_type"] in MaterialService.SUPPORTED_FORMATS
            else "standard_card"
        )
        creative_id = self.materials.insert_material_in_conn(
            conn,
            advertiser_account_id=offer["advertiser_account_id"],
            format_type=material_format,
            text=offer["creative_text"],
            target_url=offer["target_url"],
            button_text=offer["button_text"],
            category=offer["category"],
            campaign_id=campaign_id,
        )
        conn.execute(
            """
            INSERT INTO ad_orders (
                id, campaign_id, creative_id, advertiser_account_id, channel_id, slot_id,
                status, budget_cents, reserved_cents, unit_price_cents, start_at, end_at,
                scheduled_at, frequency_per_day, price_offer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                campaign_id,
                creative_id,
                offer["advertiser_account_id"],
                offer["channel_id"],
                rate["slot_id"],
                budget_cents,
                budget_cents,
                offer["offered_price_cents"],
                scheduled_at,
                offer["end_at"],
                scheduled_at,
                offer["frequency_per_day"] or 1,
                offer["id"],
            ),
        )
        self.ledger.reserve_budget(conn, offer["advertiser_account_id"], order_id, budget_cents, rate["currency"])
        self._snapshot(
            conn,
            order_id,
            None,
            "creative",
            {
                "text": offer["creative_text"],
                "target_url": offer["target_url"],
                "button_text": offer["button_text"],
                "format_type": material_format,
                "material_id": creative_id,
            },
        )
        self._snapshot(conn, order_id, None, "accepted_price_offer", dict(offer))
        self._snapshot(conn, order_id, None, "rate_card", {**rate, "accepted_unit_price_cents": offer["offered_price_cents"], "price_offer_id": offer["id"]})
        self._snapshot(conn, order_id, None, "channel", channel)
        return order_id

    def _snapshot(
        self,
        conn: sqlite3.Connection,
        order_id: str | None,
        delivery_id: str | None,
        snapshot_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO evidence_snapshots (id, order_id, delivery_id, snapshot_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id("ev"), order_id, delivery_id, snapshot_type, json.dumps(payload, ensure_ascii=False, default=str)),
        )
