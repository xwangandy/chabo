from __future__ import annotations

import json
import time
from typing import Callable

from .bot import UpdateHandler
from .config import Settings
from .db import Database
from .fulfillment import FulfillmentService
from .telegram import BotApiClient, TelegramError


class PollingRunner:
    def __init__(self, settings: Settings):
        if not settings.bot_token:
            raise TelegramError("CHABO_BOT_TOKEN is not configured")
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.init()
        self.gateway = BotApiClient(settings.bot_token, settings.telegram_http_backend)
        self.handler = UpdateHandler(self.db, settings, self.gateway)
        self.fulfillment = FulfillmentService(self.db, settings, self.gateway)

    def run(
        self,
        *,
        timeout: int = 30,
        limit: int = 100,
        once: bool = False,
        drop_pending_updates: bool = False,
        idle_sleep_seconds: float = 1.0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.gateway.delete_webhook(drop_pending_updates=drop_pending_updates)
        offset = self._load_offset()
        log(json.dumps({"event": "polling_started", "bot_username": self.settings.bot_username}, ensure_ascii=False))
        while True:
            try:
                updates = self.gateway.get_updates(offset=offset, timeout=timeout, limit=limit)
            except TelegramError as exc:
                log(json.dumps({"event": "polling_error", "error": str(exc)}, ensure_ascii=False))
                if once:
                    return
                time.sleep(idle_sleep_seconds)
                continue
            if not updates and once:
                log(json.dumps({"event": "polling_once_empty"}, ensure_ascii=False))
                return
            for update in updates:
                update_id = int(update["update_id"])
                try:
                    result = self.handler.handle(update)
                    log(json.dumps({"event": "update_handled", "update_id": update_id, "result": result}, ensure_ascii=False, default=str))
                except Exception as exc:
                    log(json.dumps({"event": "update_failed", "update_id": update_id, "error": str(exc)}, ensure_ascii=False))
                offset = update_id + 1
                self._save_offset(offset)
                self._dispatch_due(log)
            if once:
                return
            if not updates:
                self._dispatch_due(log)
                time.sleep(idle_sleep_seconds)

    def _load_offset(self) -> int | None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT value FROM runtime_state WHERE key = 'telegram_polling_offset'").fetchone()
            return int(row["value"]) if row else None

    def _save_offset(self, offset: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES ('telegram_polling_offset', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (str(offset),),
            )

    def _dispatch_due(self, log: Callable[[str], None]) -> None:
        try:
            result = self.fulfillment.dispatch_due()
        except Exception as exc:
            log(json.dumps({"event": "dispatch_due_failed", "error": str(exc)}, ensure_ascii=False))
            return
        if result:
            log(json.dumps({"event": "dispatch_due", "result": result}, ensure_ascii=False, default=str))
