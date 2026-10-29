import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.main import _retry_schema
from app.nats_worker import NatsWorker


class StoreStub:
    available = True
    schema_ready = True


class SchemaRetryTest(unittest.TestCase):
    def test_schema_setup_retries_after_transient_failure(self):
        stop = threading.Event()

        class Store:
            schema_ready = False
            calls = 0

            def ensure_schema(self):
                self.calls += 1
                if self.calls == 2:
                    self.schema_ready = True
                else:
                    raise OSError("temporary db outage")

        store = Store()
        thread = threading.Thread(target=_retry_schema, args=(stop, store, 0.01))
        thread.start()
        thread.join(timeout=1)
        stop.set()

        self.assertFalse(thread.is_alive())
        self.assertTrue(store.schema_ready)
        self.assertEqual(store.calls, 2)

    def test_schema_retry_stops_promptly_on_shutdown(self):
        stop = threading.Event()

        class Store:
            schema_ready = False
            calls = 0

            def ensure_schema(self):
                self.calls += 1
                raise OSError("temporary db outage")

        store = Store()
        thread = threading.Thread(target=_retry_schema, args=(stop, store, 1))
        thread.start()
        for _ in range(100):
            if store.calls:
                break
            stop.wait(0.005)
        stop.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())


class ConnectionStub:
    is_connected = True

    def __init__(self, js):
        self.js = js
        self.closed = False

    def jetstream(self):
        return self.js

    async def close(self):
        self.closed = True


class JetStreamStub:
    def __init__(self, fail_cleanup=False):
        self.fail_cleanup = fail_cleanup
        self.subscriptions = []

    async def pull_subscribe(self, subject, **_kwargs):
        self.subscriptions.append(subject)
        if self.fail_cleanup and subject == "app.account.deletion_requested.v1":
            raise OSError("subscription unavailable")
        return object()


class NatsLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_connect_retries_and_shutdown_clears_ready_state(self):
        js = JetStreamStub()
        connection = ConnectionStub(js)
        worker = NatsWorker(Settings(nats_url="nats://local", internal_service_token="token"), object(), StoreStub())
        connect = AsyncMock(side_effect=[OSError("startup outage"), connection])

        async def stop_consumers(*_args):
            await asyncio.sleep(0.01)
            worker._stop.set()

        async def wait_for_stop(*_args):
            while not worker._stop.is_set():
                await asyncio.sleep(0.01)

        worker._consume_due = stop_consumers
        worker._consume_cleanup = wait_for_stop
        async def short_retry():
            await asyncio.sleep(0.01)

        worker._wait_before_retry = short_retry
        with patch("app.nats_worker.nats.connect", connect):
            await worker._run()

        self.assertEqual(connect.await_count, 2)
        self.assertEqual(len(js.subscriptions), 2)
        self.assertTrue(connection.closed)
        self.assertFalse(worker.connected)

    async def test_failed_second_subscription_closes_connection_before_retry(self):
        js = JetStreamStub(fail_cleanup=True)
        connection = ConnectionStub(js)
        worker = NatsWorker(Settings(nats_url="nats://local", internal_service_token="token"), object(), StoreStub())
        connect = AsyncMock(return_value=connection)

        async def stop_retry():
            worker._stop.set()

        worker._wait_before_retry = stop_retry
        with patch("app.nats_worker.nats.connect", connect):
            await worker._run()

        self.assertTrue(connection.closed)
        self.assertFalse(worker.connected)
        self.assertEqual(js.subscriptions, ["app.reminders.due.v1", "app.account.deletion_requested.v1"])


if __name__ == "__main__":
    unittest.main()
