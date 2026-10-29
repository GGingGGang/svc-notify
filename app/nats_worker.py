from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone

import nats

from app.config import Settings
from app.service import NotifyService
from app.store import Store

logger = logging.getLogger(__name__)


class NatsWorker:
    def __init__(self, settings: Settings, service: NotifyService, store: Store) -> None:
        self.settings = settings
        self.service = service
        self.store = store
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False

    @property
    def configured(self) -> bool:
        return bool(self.settings.nats_url and self.store.available and self.store.schema_ready
                    and self.settings.internal_service_token)

    def start(self) -> None:
        if self.configured and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), name="notify-nats", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                connection = await nats.connect(self.settings.nats_url, connect_timeout=2, max_reconnect_attempts=-1)
                self.connected = True
                js = connection.jetstream()
                due = await js.pull_subscribe("app.reminders.due.v1", durable="notify-reminder-dispatch", stream="APP_REMINDERS")
                cleanup = await js.pull_subscribe("app.account.deletion_requested.v1", durable="notify-account-cleanup", stream="ACCOUNT_LIFECYCLE")
                await asyncio.gather(self._consume_due(due, js), self._consume_cleanup(cleanup, js), self._monitor_connection(connection))
                await connection.close()
            except Exception as exc:
                self.connected = False
                logger.warning("notification event connection unavailable (%s)", type(exc).__name__)
                await asyncio.sleep(2)

    async def _monitor_connection(self, connection) -> None:
        while not self._stop.is_set() and connection.is_connected:
            await asyncio.sleep(0.5)
        if not self._stop.is_set():
            raise RuntimeError("NATS disconnected")

    async def _consume_due(self, subscription, js) -> None:
        while not self._stop.is_set():
            try:
                messages = await subscription.fetch(1, timeout=1)
            except asyncio.TimeoutError:
                continue
            except Exception:
                if not self._stop.is_set():
                    raise
                return
            for message in messages:
                try:
                    event = json.loads(message.data)
                    result = self.service.deliver(event)
                    if result.get("in_flight"):
                        await message.nak(delay=10)
                        continue
                    await _publish_result(js, result)
                    await message.ack()
                except Exception as exc:
                    logger.warning("notification delivery processing failed (%s)", type(exc).__name__)
                    await message.nak(delay=2)

    async def _consume_cleanup(self, subscription, js) -> None:
        while not self._stop.is_set():
            try:
                messages = await subscription.fetch(1, timeout=1)
            except asyncio.TimeoutError:
                continue
            except Exception:
                if not self._stop.is_set():
                    raise
                return
            for message in messages:
                try:
                    event = json.loads(message.data)
                    event_id, request_id, user_id = event["event_id"], event["request_id"], event["user_id"]
                    self.store.delete_user(user_id, event_id)
                    result = {
                        "event_id": event_id,
                        "request_id": request_id,
                        "service": "notify",
                        "status": "completed",
                        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                    await js.publish("app.account.deletion_result.v1", json.dumps(result).encode(),
                                     headers={"Nats-Msg-Id": f"account-delete:notify:{event_id}", "content-type": "application/json"})
                    await message.ack()
                except Exception as exc:
                    logger.warning("account cleanup processing failed (%s)", type(exc).__name__)
                    await message.nak(delay=2)


async def _publish_result(js, result: dict) -> None:
    payload = json.dumps(result, separators=(",", ":")).encode("utf-8")
    message_id = f"reminder-result:{result.get('dispatch_id')}:{result.get('attempt')}"
    await js.publish("app.reminders.result.v1", payload,
                     headers={"Nats-Msg-Id": message_id, "content-type": "application/json"})
