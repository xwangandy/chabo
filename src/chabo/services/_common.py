from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ChaboError(RuntimeError):
    pass


class NotFound(ChaboError):
    pass


class InsufficientBalance(ChaboError):
    pass


class InvalidState(ChaboError):
    pass


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _audit_payload_with_note(base: dict[str, Any], note: str | None) -> dict[str, Any]:
    """Merge an operator note into an audit payload.

    Notes are written into ``audit_logs.payload_json["note"]`` so detail
    pages and exports can render them on the same timeline as the rest of
    the action's payload. Empty / whitespace-only notes are dropped.
    """
    cleaned = (note or "").strip()
    if not cleaned:
        return base
    return {**base, "note": cleaned}
