from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    telegram_user_id TEXT UNIQUE,
    role TEXT NOT NULL,
    display_name TEXT,
    available_balance_cents INTEGER NOT NULL DEFAULT 0,
    reserved_balance_cents INTEGER NOT NULL DEFAULT 0,
    spent_balance_cents INTEGER NOT NULL DEFAULT 0,
    pending_earnings_cents INTEGER NOT NULL DEFAULT 0,
    confirmed_earnings_cents INTEGER NOT NULL DEFAULT 0,
    releasable_earnings_cents INTEGER NOT NULL DEFAULT 0,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    timezone_confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (available_balance_cents >= 0),
    CHECK (reserved_balance_cents >= 0),
    CHECK (spent_balance_cents >= 0),
    CHECK (pending_earnings_cents >= 0),
    CHECK (confirmed_earnings_cents >= 0),
    CHECK (releasable_earnings_cents >= 0)
);

CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    telegram_chat_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    username TEXT,
    ref_token TEXT NOT NULL UNIQUE,
    owner_account_id TEXT NOT NULL REFERENCES accounts(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_configs (
    channel_id TEXT PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    daily_ad_limit INTEGER NOT NULL DEFAULT 3,
    allowed_start_hour INTEGER NOT NULL DEFAULT 9,
    allowed_end_hour INTEGER NOT NULL DEFAULT 23,
    allow_pin INTEGER NOT NULL DEFAULT 1,
    category_blocklist_json TEXT NOT NULL DEFAULT '[]',
    service_fee_bps INTEGER NOT NULL DEFAULT 500,
    holdback_bps INTEGER NOT NULL DEFAULT 1000,
    holdback_days INTEGER NOT NULL DEFAULT 7,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_admins (
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    telegram_user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    display_name TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    can_post_messages INTEGER NOT NULL DEFAULT 0,
    can_edit_messages INTEGER NOT NULL DEFAULT 0,
    can_pin_messages INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, telegram_user_id)
);

CREATE TABLE IF NOT EXISTS ad_slots (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    slot_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    min_days INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, slot_type)
);

CREATE TABLE IF NOT EXISTS rate_cards (
    id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL REFERENCES ad_slots(id) ON DELETE CASCADE,
    currency TEXT NOT NULL DEFAULT 'USD',
    unit_price_cents INTEGER NOT NULL,
    pricing_unit TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_ad_format_policies (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    format_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    owner_price_band TEXT NOT NULL DEFAULT 'medium',
    platform_promo_enabled INTEGER NOT NULL DEFAULT 1,
    custom_multiplier_bps INTEGER,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, format_type),
    CHECK (owner_price_band IN ('low', 'medium', 'high', 'custom'))
);

CREATE TABLE IF NOT EXISTS channel_pricing_assessments (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    median_24h_views INTEGER NOT NULL DEFAULT 0,
    subscribers INTEGER NOT NULL DEFAULT 0,
    light_clicks_30d INTEGER NOT NULL DEFAULT 0,
    light_unique_clickers_30d INTEGER NOT NULL DEFAULT 0,
    repeat_purchase_count INTEGER NOT NULL DEFAULT 0,
    dispute_count INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT 'normal',
    base_standard_price_cents INTEGER NOT NULL,
    score INTEGER NOT NULL,
    breakdown_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_subscriptions (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    plan TEXT NOT NULL DEFAULT 'publisher_premium',
    subscriber_count INTEGER NOT NULL DEFAULT 0,
    monthly_price_cents INTEGER NOT NULL,
    starts_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('active', 'expired', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS price_offers (
    id TEXT PRIMARY KEY,
    advertiser_account_id TEXT NOT NULL REFERENCES accounts(id),
    channel_id TEXT NOT NULL REFERENCES channels(id),
    slot_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    offered_price_cents INTEGER NOT NULL,
    list_price_cents INTEGER NOT NULL,
    budget_cents INTEGER NOT NULL,
    creative_text TEXT NOT NULL,
    target_url TEXT NOT NULL,
    button_text TEXT NOT NULL DEFAULT '查看详情',
    category TEXT NOT NULL DEFAULT 'general',
    scheduled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    end_at TEXT,
    frequency_per_day INTEGER NOT NULL DEFAULT 1,
    accepted_order_id TEXT REFERENCES ad_orders(id),
    message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    responded_at TEXT,
    CHECK (status IN ('pending', 'accepted', 'rejected', 'expired'))
);

CREATE TABLE IF NOT EXISTS light_probes (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    short_text TEXT NOT NULL,
    detail_text TEXT NOT NULL,
    target_url TEXT NOT NULL,
    button_text TEXT NOT NULL DEFAULT '了解详情',
    start_at TEXT,
    end_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('active', 'paused', 'ended'))
);

CREATE TABLE IF NOT EXISTS light_probe_events (
    id TEXT PRIMARY KEY,
    probe_id TEXT NOT NULL REFERENCES light_probes(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    telegram_user_id TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'bot_start',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS advertiser_saved_channels (
    id TEXT PRIMARY KEY,
    advertiser_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(advertiser_account_id, channel_id)
);

CREATE TABLE IF NOT EXISTS advertiser_alert_rules (
    id TEXT PRIMARY KEY,
    advertiser_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    category TEXT,
    min_score INTEGER NOT NULL DEFAULT 70,
    max_risk_level TEXT NOT NULL DEFAULT 'normal',
    max_price_cents INTEGER,
    slot_type TEXT NOT NULL DEFAULT 'standard_card',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('active', 'paused')),
    CHECK (max_risk_level IN ('normal', 'watch', 'high', 'blocked'))
);

CREATE TABLE IF NOT EXISTS advertiser_alert_events (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL REFERENCES advertiser_alert_rules(id) ON DELETE CASCADE,
    advertiser_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    assessment_id TEXT NOT NULL REFERENCES channel_pricing_assessments(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(rule_id, channel_id, assessment_id),
    CHECK (status IN ('new', 'read', 'dismissed'))
);

CREATE TABLE IF NOT EXISTS advertiser_subscriptions (
    id TEXT PRIMARY KEY,
    advertiser_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    plan TEXT NOT NULL,
    monthly_price_cents INTEGER NOT NULL,
    starts_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('active', 'expired', 'cancelled')),
    CHECK (plan IN ('pro', 'enterprise'))
);

CREATE TABLE IF NOT EXISTS stars_payment_intents (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL UNIQUE,
    buyer_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    telegram_user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    currency TEXT NOT NULL DEFAULT 'XTR',
    stars_amount INTEGER NOT NULL,
    internal_amount_cents INTEGER NOT NULL,
    target_channel_id TEXT REFERENCES channels(id) ON DELETE CASCADE,
    plan TEXT,
    months INTEGER NOT NULL DEFAULT 1,
    subscriber_count INTEGER,
    telegram_payment_charge_id TEXT UNIQUE,
    provider_payment_charge_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at TEXT,
    fulfilled_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (kind IN ('balance_topup', 'publisher_subscription', 'advertiser_subscription')),
    CHECK (status IN ('pending', 'paid', 'fulfilled', 'rejected', 'expired')),
    CHECK (currency = 'XTR'),
    CHECK (stars_amount > 0),
    CHECK (internal_amount_cents > 0),
    CHECK (months >= 1)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    advertiser_account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    objective TEXT NOT NULL DEFAULT 'channel_ad',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS creatives (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    advertiser_account_id TEXT REFERENCES accounts(id),
    format_type TEXT NOT NULL DEFAULT 'standard_card',
    text TEXT NOT NULL,
    target_url TEXT NOT NULL,
    button_text TEXT NOT NULL DEFAULT '查看详情',
    category TEXT NOT NULL DEFAULT 'general',
    light_short_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review',
    content_hash TEXT NOT NULL,
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ad_orders (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    creative_id TEXT NOT NULL REFERENCES creatives(id),
    advertiser_account_id TEXT NOT NULL REFERENCES accounts(id),
    channel_id TEXT NOT NULL REFERENCES channels(id),
    slot_id TEXT NOT NULL REFERENCES ad_slots(id),
    status TEXT NOT NULL DEFAULT 'pending_review',
    currency TEXT NOT NULL DEFAULT 'USD',
    budget_cents INTEGER NOT NULL,
    reserved_cents INTEGER NOT NULL,
    spent_cents INTEGER NOT NULL DEFAULT 0,
    unit_price_cents INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    scheduled_at TEXT NOT NULL,
    frequency_per_day INTEGER NOT NULL DEFAULT 1,
    price_offer_id TEXT REFERENCES price_offers(id),
    low_budget_notified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (budget_cents >= 0),
    CHECK (reserved_cents >= 0),
    CHECK (spent_cents >= 0)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES ad_orders(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    creative_id TEXT NOT NULL REFERENCES creatives(id),
    status TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_at TEXT NOT NULL,
    sent_at TEXT,
    message_id TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    charge_cents INTEGER NOT NULL DEFAULT 0,
    publisher_net_cents INTEGER NOT NULL DEFAULT 0,
    platform_fee_cents INTEGER NOT NULL DEFAULT 0,
    refunded_cents INTEGER NOT NULL DEFAULT 0,
    publisher_reversed_cents INTEGER NOT NULL DEFAULT 0,
    platform_fee_reversed_cents INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id TEXT PRIMARY KEY,
    delivery_id TEXT REFERENCES deliveries(id) ON DELETE CASCADE,
    metric_type TEXT NOT NULL,
    value INTEGER NOT NULL,
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    related_account_id TEXT REFERENCES accounts(id),
    order_id TEXT REFERENCES ad_orders(id),
    delivery_id TEXT REFERENCES deliveries(id),
    type TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    amount_cents INTEGER NOT NULL,
    available_after_cents INTEGER NOT NULL,
    reserved_after_cents INTEGER NOT NULL,
    spent_after_cents INTEGER NOT NULL,
    pending_after_cents INTEGER NOT NULL,
    confirmed_after_cents INTEGER NOT NULL,
    releasable_after_cents INTEGER NOT NULL,
    memo TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payout_batches (
    id TEXT PRIMARY KEY,
    publisher_account_id TEXT NOT NULL REFERENCES accounts(id),
    status TEXT NOT NULL DEFAULT 'draft',
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at TEXT
);

CREATE TABLE IF NOT EXISTS disputes (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES ad_orders(id),
    delivery_id TEXT REFERENCES deliveries(id),
    opened_by_account_id TEXT NOT NULL REFERENCES accounts(id),
    status TEXT NOT NULL DEFAULT 'open',
    reason TEXT NOT NULL,
    resolution TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS evidence_snapshots (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES ad_orders(id),
    delivery_id TEXT REFERENCES deliveries(id),
    snapshot_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    actor_account_id TEXT REFERENCES accounts(id),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS advertiser_sessions (
    id TEXT PRIMARY KEY,
    advertiser_account_id TEXT NOT NULL REFERENCES accounts(id),
    ref_channel_id TEXT REFERENCES channels(id),
    start_payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS topup_requests (
    id TEXT PRIMARY KEY,
    recipient_telegram_user_id TEXT NOT NULL,
    recipient_account_id TEXT REFERENCES accounts(id),
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    reason TEXT NOT NULL,
    evidence_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requester_account_id TEXT NOT NULL REFERENCES accounts(id),
    request_note TEXT,
    approver_account_id TEXT REFERENCES accounts(id),
    approval_note TEXT,
    settled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (amount_cents > 0),
    CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_topup_requests_status ON topup_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_topup_requests_recipient ON topup_requests(recipient_telegram_user_id, created_at);

CREATE TABLE IF NOT EXISTS tool_call_logs (
    id TEXT PRIMARY KEY,
    actor_account_id TEXT REFERENCES accounts(id),
    actor_telegram_user_id TEXT,
    actor_kind TEXT NOT NULL DEFAULT 'human',
    session_id TEXT,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    result_status TEXT NOT NULL,
    result_summary TEXT,
    error_type TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (actor_kind IN ('human', 'ai', 'admin', 'system')),
    CHECK (result_status IN ('success', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_tool_call_logs_actor ON tool_call_logs(actor_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_call_logs_telegram ON tool_call_logs(actor_telegram_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_call_logs_tool ON tool_call_logs(tool_name, created_at);

CREATE TABLE IF NOT EXISTS self_promo_publishes (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    creative_id TEXT NOT NULL REFERENCES creatives(id),
    publisher_account_id TEXT NOT NULL REFERENCES accounts(id),
    message_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('pending', 'sent', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_self_promo_channel ON self_promo_publishes(channel_id, created_at);

CREATE TABLE IF NOT EXISTS bot_conversation_states (
    chat_id TEXT PRIMARY KEY,
    account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    flow TEXT NOT NULL,
    step TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON ad_orders(status);
CREATE INDEX IF NOT EXISTS idx_channel_admins_user ON channel_admins(telegram_user_id, status);
CREATE INDEX IF NOT EXISTS idx_deliveries_due ON deliveries(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_transactions(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON advertiser_sessions(advertiser_account_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_pricing_assessments_channel ON channel_pricing_assessments(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_price_offers_channel ON price_offers(channel_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_channel_subscriptions_channel ON channel_subscriptions(channel_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_light_probes_channel ON light_probes(channel_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_light_probe_events_probe ON light_probe_events(probe_id, created_at);
CREATE INDEX IF NOT EXISTS idx_saved_channels_advertiser ON advertiser_saved_channels(advertiser_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_rules_advertiser ON advertiser_alert_rules(advertiser_account_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_events_advertiser ON advertiser_alert_events(advertiser_account_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_advertiser_subscriptions_account ON advertiser_subscriptions(advertiser_account_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_stars_payment_intents_buyer ON stars_payment_intents(buyer_account_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_bot_conversations_account ON bot_conversation_states(account_id, updated_at);
"""


class Database:
    def __init__(self, path: str):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        is_memory = self.path == ":memory:"
        if not is_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def backup_to(self, target_path: str) -> str:
        """Dump the live SQLite DB to target_path using the online backup API.

        Returns the absolute path of the written file. Safe to run while the
        primary process is serving — SQLite holds locks long enough to
        get a consistent snapshot but does not block writers for the full
        duration of the copy.
        """
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        with self.connect() as source, sqlite3.connect(str(target)) as backup:
            source.backup(backup)
        return str(target.resolve())

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply numbered migrations once each, tracked in schema_migrations.

        Migrations are listed in MIGRATIONS at module scope, ordered by id.
        A migration that touches an already-up-to-date schema is still safe
        to run because each step is itself idempotent (ALTER TABLE wrapped
        in duplicate-column suppression, UPDATEs guarded by WHERE clauses,
        CREATE INDEX IF NOT EXISTS). The schema_migrations table records
        which ids have been applied so future non-idempotent migrations
        only run once.
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()}
        for mig_id, mig_fn in MIGRATIONS:
            if mig_id in applied:
                continue
            mig_fn(conn)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (mig_id,))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# --- Numbered migrations ---------------------------------------------------
# Each entry is (id, callable). The callable receives a sqlite3.Connection
# and must apply the change idempotently. New migrations append a new (id,
# fn) tuple — never edit a published id, and never reorder.

def _add_columns_idempotent(conn: sqlite3.Connection, statements: list[str]) -> None:
    for stmt in statements:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _0001_price_offers_v2(conn: sqlite3.Connection) -> None:
    _add_columns_idempotent(conn, [
        "ALTER TABLE price_offers ADD COLUMN budget_cents INTEGER",
        "ALTER TABLE price_offers ADD COLUMN creative_text TEXT",
        "ALTER TABLE price_offers ADD COLUMN target_url TEXT",
        "ALTER TABLE price_offers ADD COLUMN button_text TEXT DEFAULT '查看详情'",
        "ALTER TABLE price_offers ADD COLUMN category TEXT DEFAULT 'general'",
        "ALTER TABLE price_offers ADD COLUMN scheduled_at TEXT",
        "ALTER TABLE price_offers ADD COLUMN end_at TEXT",
        "ALTER TABLE price_offers ADD COLUMN frequency_per_day INTEGER DEFAULT 1",
        "ALTER TABLE price_offers ADD COLUMN accepted_order_id TEXT REFERENCES ad_orders(id)",
    ])


def _0002_orders_price_offer_link(conn: sqlite3.Connection) -> None:
    _add_columns_idempotent(conn, [
        "ALTER TABLE ad_orders ADD COLUMN price_offer_id TEXT REFERENCES price_offers(id)",
    ])


def _0003_deliveries_partial_refund(conn: sqlite3.Connection) -> None:
    _add_columns_idempotent(conn, [
        "ALTER TABLE deliveries ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE deliveries ADD COLUMN publisher_reversed_cents INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE deliveries ADD COLUMN platform_fee_reversed_cents INTEGER NOT NULL DEFAULT 0",
    ])


def _0004_accounts_timezone(conn: sqlite3.Connection) -> None:
    _add_columns_idempotent(conn, [
        "ALTER TABLE accounts ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'",
        "ALTER TABLE accounts ADD COLUMN timezone_confirmed_at TEXT",
    ])


def _0005_creatives_library_columns(conn: sqlite3.Connection) -> None:
    _add_columns_idempotent(conn, [
        "ALTER TABLE creatives ADD COLUMN advertiser_account_id TEXT REFERENCES accounts(id)",
        "ALTER TABLE creatives ADD COLUMN format_type TEXT NOT NULL DEFAULT 'standard_card'",
        "ALTER TABLE creatives ADD COLUMN light_short_text TEXT",
        "ALTER TABLE creatives ADD COLUMN archived_at TEXT",
    ])


def _0006_creative_library_backfill(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE creatives
        SET advertiser_account_id = (
            SELECT advertiser_account_id FROM campaigns
            WHERE campaigns.id = creatives.campaign_id
        )
        WHERE advertiser_account_id IS NULL
        """
    )
    conn.execute(
        """
        UPDATE creatives
        SET format_type = COALESCE((
            SELECT ad_slots.slot_type FROM ad_orders
            JOIN ad_slots ON ad_slots.id = ad_orders.slot_id
            WHERE ad_orders.creative_id = creatives.id
            ORDER BY ad_orders.created_at ASC
            LIMIT 1
        ), 'standard_card')
        WHERE format_type = 'standard_card'
          AND EXISTS (
            SELECT 1 FROM ad_orders WHERE ad_orders.creative_id = creatives.id
          )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_creatives_advertiser ON creatives(advertiser_account_id, archived_at, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_creatives_format ON creatives(advertiser_account_id, format_type, archived_at)"
    )


MIGRATIONS: list[tuple[str, "Callable[[sqlite3.Connection], None]"]] = [
    ("0001_price_offers_v2", _0001_price_offers_v2),
    ("0002_orders_price_offer_link", _0002_orders_price_offer_link),
    ("0003_deliveries_partial_refund", _0003_deliveries_partial_refund),
    ("0004_accounts_timezone", _0004_accounts_timezone),
    ("0005_creatives_library_columns", _0005_creatives_library_columns),
    ("0006_creative_library_backfill", _0006_creative_library_backfill),
]
