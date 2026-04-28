from __future__ import annotations

from dataclasses import dataclass

from .bot import UpdateHandler
from .config import Settings
from .db import Database
from .fulfillment import FulfillmentService
from .services import (
    AdvertiserService,
    AdvertiserSubscriptionService,
    ChannelService,
    DisputeService,
    LedgerService,
    LightProbeService,
    MaterialService,
    OrderService,
    PriceOfferService,
    PricingService,
    SelfPromoService,
    StarsPaymentService,
    SubscriptionService,
    ToolCallLogService,
)
from .telegram import BotApiClient, MessageGateway, NullGateway


@dataclass
class ChaboApp:
    settings: Settings
    db: Database
    gateway: MessageGateway
    channels: ChannelService
    ledger: LedgerService
    materials: MaterialService
    orders: OrderService
    disputes: DisputeService
    pricing: PricingService
    price_offers: PriceOfferService
    subscriptions: SubscriptionService
    stars_payments: StarsPaymentService
    light_probes: LightProbeService
    self_promos: SelfPromoService
    tool_call_logs: ToolCallLogService
    advertiser_subscriptions: AdvertiserSubscriptionService
    advertisers: AdvertiserService
    fulfillment: FulfillmentService
    update_handler: UpdateHandler


def create_app(settings: Settings | None = None, gateway: MessageGateway | None = None) -> ChaboApp:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    db.init()
    if gateway is None:
        gateway = BotApiClient(settings.bot_token, settings.telegram_http_backend) if settings.bot_token else NullGateway()
    return ChaboApp(
        settings=settings,
        db=db,
        gateway=gateway,
        channels=ChannelService(db, settings),
        ledger=LedgerService(db, settings),
        materials=MaterialService(db, settings),
        orders=OrderService(db, settings),
        disputes=DisputeService(db, settings),
        pricing=PricingService(db, settings),
        price_offers=PriceOfferService(db, settings),
        subscriptions=SubscriptionService(db, settings),
        stars_payments=StarsPaymentService(db, settings),
        light_probes=LightProbeService(db, settings),
        self_promos=SelfPromoService(db, settings),
        tool_call_logs=ToolCallLogService(db, settings),
        advertiser_subscriptions=AdvertiserSubscriptionService(db, settings),
        advertisers=AdvertiserService(db, settings),
        fulfillment=FulfillmentService(db, settings, gateway),
        update_handler=UpdateHandler(db, settings, gateway),
    )
