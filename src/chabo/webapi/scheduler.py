from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI

from ..app import ChaboApp

logger = logging.getLogger(__name__)


def run_periodic_jobs_once(app: ChaboApp) -> dict[str, Any]:
    """
    Execute one pass of ChaBo's fulfillment & settlement jobs:
    - dispatch_due (scheduled -> sent)
    - confirm_due_earnings (sent -> confirmed after 24h)
    - release_due_holdbacks (release held back publisher funds after holdback_days)
    - unpublish_due (unpin pin24h ads after 24h)
    - sync_delivery_metrics (sample post views via TelegramABC if configured)
    """
    results: dict[str, Any] = {}
    try:
        dispatched = app.fulfillment.dispatch_due(limit=20)
        results["dispatched_count"] = len(dispatched)
    except Exception as exc:
        logger.warning("scheduler_dispatch_due_failed error=%s", exc)
        results["dispatched_error"] = str(exc)

    try:
        results["confirmed_count"] = app.fulfillment.confirm_due_earnings(observation_hours=24)
    except Exception as exc:
        logger.warning("scheduler_confirm_earnings_failed error=%s", exc)
        results["confirmed_error"] = str(exc)

    try:
        results["released_holdbacks_count"] = app.fulfillment.release_due_holdbacks()
    except Exception as exc:
        logger.warning("scheduler_release_holdbacks_failed error=%s", exc)
        results["released_holdbacks_error"] = str(exc)

    try:
        results["unpinned_count"] = app.fulfillment.unpublish_due(pin_hours=24)
    except Exception as exc:
        logger.warning("scheduler_unpublish_due_failed error=%s", exc)
        results["unpinned_error"] = str(exc)

    try:
        results["synced_metrics_count"] = app.fulfillment.sync_delivery_metrics(checkpoint_label="periodic")
    except Exception as exc:
        logger.warning("scheduler_sync_metrics_failed error=%s", exc)
        results["synced_metrics_error"] = str(exc)

    return results


@contextlib.asynccontextmanager
async def create_scheduler_lifespan(chabo: ChaboApp) -> AsyncIterator[None]:
    """Optional background loop enabled when CHABO_ENABLE_SCHEDULER=1."""
    enabled = (os.environ.get("CHABO_ENABLE_SCHEDULER") or "").strip().lower() in {"1", "true", "yes"}
    interval_sec = max(10, int(os.environ.get("CHABO_SCHEDULER_INTERVAL_SEC") or "60"))
    task: asyncio.Task[None] | None = None

    async def _loop() -> None:
        while True:
            try:
                await asyncio.to_thread(run_periodic_jobs_once, chabo)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("background_scheduler_tick_failed error=%s", exc)
            await asyncio.sleep(interval_sec)

    if enabled:
        task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
