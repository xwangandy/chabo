from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramABCClient:
    """
    Read-only HTTP client for the TelegramABC gateway (/api/plugins/assets/*).
    Never holds MTProto session files directly; degrades gracefully when offline.
    """

    base_url: str
    plugin_token: str
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> TelegramABCClient:
        return cls(
            base_url=(os.environ.get("TELEGRAM_ABC_BASE_URL") or "").strip().rstrip("/"),
            plugin_token=(os.environ.get("TELEGRAM_ABC_PLUGIN_TOKEN") or "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.plugin_token)

    def _get_json(self, path: str) -> Any | None:
        if not self.is_configured:
            return None
        url = f"{self.base_url}/{path.lstrip('/')}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Plugin-Token": self.plugin_token,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            logger.warning("telegram_abc_request_failed url=%s error=%s", url, exc)
            return None

    def fetch_channels(self) -> list[dict[str, Any]]:
        """Fetch owned channel inventory from TelegramABC GET /api/plugins/assets/channels."""
        payload = self._get_json("/api/plugins/assets/channels")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return [item for item in payload["items"] if isinstance(item, dict)]
        return []

    def fetch_message_views(
        self,
        *,
        telegram_chat_id: str | int,
        message_id: str | int,
    ) -> dict[str, int] | None:
        """Fetch post views and forwards from TelegramABC for a delivered ad post."""
        payload = self._get_json(
            f"/api/plugins/assets/channels/{telegram_chat_id}/messages/{message_id}"
        )
        if not isinstance(payload, dict):
            return None
        views = payload.get("views")
        if views is None:
            views = payload.get("views_count")
        if views is None:
            return None
        forwards = payload.get("forwards") or payload.get("forwards_count") or 0
        return {
            "views": max(0, int(views)),
            "forwards": max(0, int(forwards)),
        }


def sync_channels_from_abc(
    app: Any,
    *,
    owner_telegram_user_id: str | int = "10001",
    owner_display_name: str = "TelegramABC 同步频道主",
    abc_client: TelegramABCClient | None = None,
    channels_override: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Sync owned channel inventory from TelegramABC into ChaBo channels & assessments.
    Degrades gracefully to empty list when TelegramABC is offline.
    """
    client = abc_client or TelegramABCClient.from_env()
    raw_items = channels_override if channels_override is not None else client.fetch_channels()
    synced: list[dict[str, Any]] = []
    for item in raw_items:
        chat_id = item.get("telegram_chat_id") or item.get("chat_id") or item.get("id")
        title = (item.get("title") or "").strip()
        if not chat_id or not title:
            continue
        username = (item.get("username") or "").strip().lstrip("@") or None
        ch = app.channels.bind_channel(
            telegram_chat_id=chat_id,
            title=title,
            username=username,
            owner_telegram_user_id=owner_telegram_user_id,
            owner_display_name=owner_display_name,
        )
        subscribers = max(0, int(item.get("subscribers") or item.get("member_count") or 0))
        median_views = max(0, int(item.get("median_24h_views") or item.get("avg_views") or 0))
        category = str(item.get("category") or "general")
        assessment = app.pricing.assess_channel(
            channel_id=ch["id"],
            category=category,
            median_24h_views=median_views,
            subscribers=subscribers,
            risk_level="normal",
        )
        synced.append({**ch, "assessment_id": assessment["id"], "score": assessment["score"]})
    return synced
