from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .ids import new_id


AUDIT_HASH_VERSION = "sha256-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_json(payload: dict[str, Any] | str | None) -> str:
    if payload is None:
        return "{}"
    if isinstance(payload, str):
        try:
            return canonical_json(json.loads(payload))
        except json.JSONDecodeError:
            return canonical_json({"raw": payload})
    return canonical_json(payload)


def latest_audit_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT audit_hash
        FROM audit_logs
        WHERE audit_hash IS NOT NULL
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    return row["audit_hash"] if row else None


def compute_audit_hash(
    *,
    audit_id: str,
    actor_account_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: str,
    created_at: str,
    previous_hash: str | None,
) -> str:
    material = canonical_json(
        {
            "id": audit_id,
            "actor_account_id": actor_account_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload_json": payload,
            "created_at": created_at,
            "previous_hash": previous_hash,
            "hash_version": AUDIT_HASH_VERSION,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_audit_chain(
    conn: sqlite3.Connection,
    *,
    created_from: str | None = None,
    created_to: str | None = None,
    strict_unsigned: bool = False,
    max_issues: int = 20,
) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if created_from:
        where.append("created_at >= ?")
        params.append(created_from)
    if created_to:
        where.append("created_at <= ?")
        params.append(created_to)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT rowid, *
        FROM audit_logs
        {where_sql}
        ORDER BY created_at ASC, rowid ASC
        """,
        params,
    ).fetchall()
    issues: list[dict[str, Any]] = []
    previous_hash: str | None = None
    first_signed_hash: str | None = None
    signed_rows = 0
    unsigned_rows = 0
    unsigned_after_chain_started = 0
    invalid_hashes = 0
    broken_links = 0

    def add_issue(kind: str, row: sqlite3.Row, detail: dict[str, Any] | None = None) -> None:
        if len(issues) >= max_issues:
            return
        issues.append(
            {
                "kind": kind,
                "rowid": row["rowid"],
                "id": row["id"],
                "created_at": row["created_at"],
                "action": row["action"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                **(detail or {}),
            }
        )

    for row in rows:
        audit_hash = row["audit_hash"]
        if not audit_hash:
            unsigned_rows += 1
            if previous_hash:
                unsigned_after_chain_started += 1
            if strict_unsigned:
                add_issue("unsigned_audit_row", row)
            continue

        expected_hash = compute_audit_hash(
            audit_id=row["id"],
            actor_account_id=row["actor_account_id"],
            action=row["action"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            payload=row["payload_json"],
            created_at=row["created_at"],
            previous_hash=row["previous_hash"],
        )
        signed_rows += 1
        first_signed_hash = first_signed_hash or audit_hash
        if expected_hash != audit_hash:
            invalid_hashes += 1
            add_issue("invalid_audit_hash", row, {"expected_hash": expected_hash, "actual_hash": audit_hash})
        if previous_hash is not None and row["previous_hash"] != previous_hash:
            broken_links += 1
            add_issue("broken_previous_hash", row, {"expected_previous_hash": previous_hash, "actual_previous_hash": row["previous_hash"]})
        if previous_hash is None and not created_from and row["previous_hash"] is not None:
            broken_links += 1
            add_issue("unexpected_first_previous_hash", row, {"actual_previous_hash": row["previous_hash"]})
        previous_hash = audit_hash

    ok = invalid_hashes == 0 and broken_links == 0 and (unsigned_rows == 0 or not strict_unsigned)
    return {
        "ok": ok,
        "strict_unsigned": strict_unsigned,
        "checked_rows": len(rows),
        "signed_rows": signed_rows,
        "unsigned_rows": unsigned_rows,
        "unsigned_after_chain_started": unsigned_after_chain_started,
        "invalid_hashes": invalid_hashes,
        "broken_links": broken_links,
        "first_signed_hash": first_signed_hash,
        "chain_head": previous_hash,
        "created_from": created_from,
        "created_to": created_to,
        "issues": issues,
        "issue_count": invalid_hashes + broken_links + (unsigned_rows if strict_unsigned else 0),
        "truncated_issues": max(0, invalid_hashes + broken_links + (unsigned_rows if strict_unsigned else 0) - len(issues)),
    }


def insert_audit_log(
    conn: sqlite3.Connection,
    *,
    actor_account_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    audit_id = new_id("aud")
    created_at = conn.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
    encoded_payload = payload_json(payload)
    previous_hash = latest_audit_hash(conn)
    audit_hash = compute_audit_hash(
        audit_id=audit_id,
        actor_account_id=actor_account_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=encoded_payload,
        created_at=created_at,
        previous_hash=previous_hash,
    )
    conn.execute(
        """
        INSERT INTO audit_logs (
            id, actor_account_id, action, entity_type, entity_id, payload_json,
            created_at, previous_hash, audit_hash, hash_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit_id,
            actor_account_id,
            action,
            entity_type,
            entity_id,
            encoded_payload,
            created_at,
            previous_hash,
            audit_hash,
            AUDIT_HASH_VERSION,
        ),
    )
    return {
        "id": audit_id,
        "created_at": created_at,
        "previous_hash": previous_hash,
        "audit_hash": audit_hash,
        "hash_version": AUDIT_HASH_VERSION,
    }
