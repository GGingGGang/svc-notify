import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from app.batch import BatchClient, BatchUnavailable
from app.config import Settings
from app.core import CoreClient


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


class PreflightClientTest(unittest.TestCase):
    def setUp(self):
        self.event = {"dispatch_id": "d-1", "attempt": 2, "user_id": "u-1", "schedule_id": "s-1",
                      "schedule_revision": 3, "minutes_before": 10, "channel": "telegram",
                      "start_at": "2026-10-29T10:00:00Z", "remind_at": "2026-10-29T09:50:00Z",
                      "title": "Reminder"}
        self.settings = Settings(internal_service_token="service-secret", batch_url="http://batch.test",
                                 core_url="http://core.test")

    @patch("app.batch.urllib.request.urlopen")
    def test_batch_preflight_checks_dispatch_attempt_and_auth(self, urlopen):
        urlopen.return_value = Response({"valid": True, "user_id": "u-1", "schedule_id": "s-1",
                                         "revision": 4, "start_at": self.event["start_at"],
                                         "remind_at": self.event["remind_at"], "minutes_before": 10,
                                         "channel": "telegram", "title": "New title"})
        snapshot = BatchClient(self.settings).reminder_validity(self.event)
        request = urlopen.call_args.args[0]
        self.assertIn("/internal/reminders/d-1/validity?attempt=2", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer service-secret")
        self.assertEqual(snapshot["title"], "New title")

    @patch("app.batch.urllib.request.urlopen")
    def test_batch_mismatched_owner_fails_closed(self, urlopen):
        urlopen.return_value = Response({"valid": True, "user_id": "other", "schedule_id": "s-1",
                                         "revision": 4, "start_at": self.event["start_at"],
                                         "remind_at": self.event["remind_at"], "minutes_before": 10,
                                         "channel": "telegram", "title": "Reminder"})
        with self.assertRaises(BatchUnavailable):
            BatchClient(self.settings).reminder_validity(self.event)

    @patch("app.batch.urllib.request.urlopen")
    def test_batch_mismatched_reminder_instant_fails_closed(self, urlopen):
        urlopen.return_value = Response({"valid": True, "user_id": "u-1", "schedule_id": "s-1",
                                         "revision": 4, "start_at": self.event["start_at"],
                                         "remind_at": "2026-10-29T09:51:00Z", "minutes_before": 10,
                                         "channel": "telegram", "title": "Reminder"})
        with self.assertRaises(BatchUnavailable):
            BatchClient(self.settings).reminder_validity(self.event)

    @patch("app.batch.urllib.request.urlopen", side_effect=HTTPError("http://batch.test", 401, "unauthorized", {}, None))
    def test_batch_internal_auth_error_is_retryable_unavailable(self, _urlopen):
        with self.assertRaises(BatchUnavailable):
            BatchClient(self.settings).reminder_validity(self.event)

    @patch("app.core.urllib.request.urlopen")
    def test_core_preflight_includes_current_revision_and_exact_remind_instant(self, urlopen):
        urlopen.return_value = Response({"valid": True, "title": "Current title"})
        CoreClient(self.settings).reminder_validity(self.event, revision=4)
        request = urlopen.call_args.args[0]
        self.assertIn("revision=4", request.full_url)
        self.assertIn("remind_at=2026-10-29T09%3A50%3A00Z", request.full_url)

    @patch("app.core.urllib.request.urlopen", side_effect=HTTPError("http://core.test", 403, "forbidden", {}, None))
    def test_core_internal_auth_error_is_retryable_unavailable(self, _urlopen):
        from app.core import CoreUnavailable
        with self.assertRaises(CoreUnavailable):
            CoreClient(self.settings).reminder_validity(self.event, revision=4)


if __name__ == "__main__":
    unittest.main()
