"""ChaBo service layer.

Previously a single 4,745-line ``services.py``; now split into per-aggregate
submodules. This package re-exports every public symbol so existing call
sites (``from chabo.services import X``) keep working unchanged.

Submodule layout:

- ``_common``  errors, helpers, datetime utilities
- ``wallet``   Account, Ledger, ToolCallLog, TopupApproval
- ``channel``  Channel, LightProbe, SelfPromo, Pricing
- ``order``    Material, Order, Dispute, PriceOffer
- ``billing``  Subscription, AdvertiserSubscription, Stars, Advertiser
"""

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
    TopupApprovalService,
)
from .channel import (
    ChannelService,
    LightProbeService,
    PricingService,
    SelfPromoService,
)
from .order import (
    DisputeService,
    MaterialService,
    OrderService,
    PriceOfferService,
)
from .billing import (
    AdvertiserService,
    AdvertiserSubscriptionService,
    StarsPaymentService,
    SubscriptionService,
)

__all__ = [
    "AccountService",
    "AdvertiserService",
    "AdvertiserSubscriptionService",
    "ChaboError",
    "ChannelService",
    "DisputeService",
    "InsufficientBalance",
    "InvalidState",
    "LedgerService",
    "LightProbeService",
    "MaterialService",
    "NotFound",
    "OrderService",
    "PriceOfferService",
    "PricingService",
    "SelfPromoService",
    "StarsPaymentService",
    "SubscriptionService",
    "ToolCallLogService",
    "TopupApprovalService",
    "_audit_payload_with_note",
    "iso",
    "parse_iso",
    "row_to_dict",
    "utcnow",
]
