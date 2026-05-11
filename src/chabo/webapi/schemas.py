from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Portal = Literal["admin", "advertiser", "publisher"]
UserPortal = Literal["advertiser", "publisher"]
PortalAccessStatus = Literal["candidate", "active", "suspended", "revoked"]
AdminLevel = Literal["viewer", "operator", "finance", "super_admin"]


class DevSessionRequest(BaseModel):
    telegram_user_id: str
    display_name: str | None = None
    portals: list[Portal] = Field(default_factory=lambda: ["advertiser"])


class TelegramWebAppLoginRequest(BaseModel):
    init_data: str


class MagicLinkRequest(BaseModel):
    telegram_user_id: str
    display_name: str | None = None
    portals: list[Portal] = Field(default_factory=list)


class MagicConsumeRequest(BaseModel):
    token: str


class ImpersonationStartRequest(BaseModel):
    portal: UserPortal
    reason: str = Field(max_length=500)
    target_account_id: str | None = None
    telegram_user_id: str | None = None


class PortalAccessUpdateRequest(BaseModel):
    status: PortalAccessStatus
    reason: str = Field(min_length=2, max_length=500)
    admin_level: AdminLevel | None = None
    confirm_phrase: str | None = Field(default=None, max_length=40)


class NoteRequest(BaseModel):
    note: str | None = None


class RejectOrderRequest(BaseModel):
    reason: str
    note: str | None = None


class RefundDeliveryRequest(BaseModel):
    reason: str
    amount_cents: int | None = None
    note: str | None = None


class ResolveDisputeRequest(BaseModel):
    resolution: str
    note: str | None = None


class TopupCreateRequest(BaseModel):
    recipient_telegram_user_id: str
    amount_cents: int
    reason: str
    evidence_url: str | None = None
    request_note: str | None = None


class MaterialCreateRequest(BaseModel):
    format_type: str
    text: str
    target_url: str
    button_text: str = "查看详情"
    category: str = "general"
    light_short_text: str | None = None
    standard_text: str | None = None
    media_file_id: str | None = None
    media_type: str | None = None


class PlanCreateRequest(BaseModel):
    title: str = "未命名投放计划"
    creative_id: str | None = None


class PlanAddItemsRequest(BaseModel):
    channel_ids: list[str]
    slot_type: str = "standard_card"
    schedule_mode: str = "once"
    starts_at: str | None = None
    ends_at: str | None = None
    frequency_per_day: int = 1
    pin_enabled: bool = False


class PlanSubmitRequest(BaseModel):
    note: str | None = None


class DailyLimitRequest(BaseModel):
    daily_ad_limit: int


class FormatPolicyRequest(BaseModel):
    format_type: str
    enabled: bool
    owner_price_band: str = "medium"
    platform_promo_enabled: bool = True
    custom_multiplier_bps: int | None = None


class RateRequest(BaseModel):
    slot_type: str
    unit_price_cents: int


class AccountView(BaseModel):
    id: str
    telegram_user_id: str | None = None
    role: str
    display_name: str | None = None


class SessionView(BaseModel):
    account: AccountView
    portals: list[Portal]
    impersonator_account_id: str | None = None


class DashboardMetric(BaseModel):
    label: str
    value: int | str
    tone: Literal["neutral", "warning", "danger", "success"] = "neutral"


class ChannelMarketRow(BaseModel):
    id: str
    title: str
    username: str | None = None
    ref_token: str
    status: str
    category: str | None = None
    score: int | None = None
    risk_level: str | None = None
    standard_price_cents: int | None = None
    light_enabled: bool = False
    standard_enabled: bool = False
    strong_enabled: bool = False


class Page(BaseModel):
    items: list[dict]
    total: int
    limit: int
    offset: int
