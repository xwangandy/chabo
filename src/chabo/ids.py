from __future__ import annotations

import base64
import secrets
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def new_ref_token() -> str:
    raw = secrets.token_bytes(12)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
