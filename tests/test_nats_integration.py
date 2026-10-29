"""JetStream retry/publish regression tests; enabled against the CI NATS service."""
import asyncio
import os
import unittest
import uuid

import nats
from nats.js.api import RetentionPolicy, StreamConfig

from app.config import Settings
from app.nats_worker import NatsWorker, _publish_result


@unittest.skipUnless(os.getenv("NOTIFY_NATS_TEST") == "1", "requires the NATS JetStream CI service")
class JetStreamIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = await nats.connect(os.getenv("NATS_URL", "nats://127.0.0.1:4222"))
        self.js = self.connection.jetstream()
        suffix = uuid.uuid4().hex[:12]
        self.due_subject = f"test.notify.due.{suffix}"
        self.due_stream = f"NOTIFY_DUE_{suffix.upper()}"
        self.result_subject = "app.reminders.result.v1"
        self.result_stream = f"NOTIFY_RESULT_{suffix.upper()}"
        await self.js.add_stream(StreamConfig(name=self.due_stream, subjects=[self.due_subject],
                                              retention=RetentionPolicy.WORK_QUEUE))

    async def asyncTearDown(self):
        for stream in (self.due_stream, self.result_stream):
            try:
                await self.js.delete_stream(stream)
            except Exception:
                pass
        await self.connection.close()

    async def test_publish_failure_naks_due_then_redelivery_publishes_and_acks(self):
        durable = f"notify-test-{uuid.uuid4().hex[:10]}"
        sub = await self.js.pull_subscribe(self.due_subject, durable=durable, stream=self.due_stream)
        await self.js.publish(self.due_subject, b'{"dispatch_id":"d","attempt":1}')

        class Service:
            def __init__(self):
                self.calls = 0
                self.worker = None

            def deliver(self, _event):
                self.calls += 1
                if self.calls >= 2:
                    self.worker._stop.set()
                return {"dispatch_id": "d", "attempt": 1, "status": "accepted"}

        service = Service()
        worker = NatsWorker(Settings(), service, object())
        service.worker = worker
        task = asyncio.create_task(worker._consume_due(sub, self.js))
        # The first result publish fails because there is no matching stream;
        # create it before the explicit NAK's redelivery delay expires.
        await asyncio.sleep(0.25)
        # The worker publishes to the contract subject.
        await self.js.add_stream(StreamConfig(name=self.result_stream, subjects=[self.result_subject]))
        await asyncio.wait_for(task, timeout=8)

        self.assertGreaterEqual(service.calls, 2)
        self.assertEqual((await self.js.stream_info(self.result_stream)).state.messages, 1)
        self.assertEqual((await self.js.consumer_info(self.due_stream, durable)).num_pending, 0)

    async def test_nats_message_id_deduplicates_publish_after_ack_crash_window(self):
        await self.js.add_stream(StreamConfig(name=self.result_stream, subjects=[self.result_subject]))
        result = {"dispatch_id": "dispatch-once", "attempt": 1, "status": "accepted"}
        await _publish_result(self.js, result)
        await _publish_result(self.js, result)
        self.assertEqual((await self.js.stream_info(self.result_stream)).state.messages, 1)


if __name__ == "__main__":
    unittest.main()
