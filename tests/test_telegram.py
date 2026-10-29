import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.telegram import TelegramAdapter


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


class FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome

    def open(self, _request, timeout):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class CapturingOpener:
    def __init__(self, response):
        self.response = response
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return self.response


class TelegramAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = TelegramAdapter("fake-token", "bot_name", "fake-secret")

    @patch("app.telegram.urllib.request.build_opener")
    def test_provider_acceptance_requires_receipt(self, build_opener):
        build_opener.return_value = FakeOpener(FakeResponse({"ok": True, "result": {"message_id": 7}}))
        result = self.adapter.send_test("123")
        self.assertEqual((result.status, result.receipt_id), ("accepted", "7"))

    @patch("app.telegram.urllib.request.build_opener")
    def test_lost_provider_response_is_unknown_and_not_retryable(self, build_opener):
        build_opener.return_value = FakeOpener(URLError("connection dropped"))
        result = self.adapter.send_test("123")
        self.assertEqual((result.status, result.reason_code), ("unknown", "provider_response_lost"))

    @patch("app.telegram.urllib.request.build_opener")
    def test_reminder_has_only_minimal_schedule_content(self, build_opener):
        opener = CapturingOpener(FakeResponse({"ok": True, "result": {"message_id": 9}}))
        build_opener.return_value = opener
        event = {"title": "Review", "start_at": "2026-10-29T10:00:00Z", "schedule_id": "schedule-1",
                 "description": "private details"}
        result = self.adapter.send_reminder("123", event, {"timezone": "UTC"}, "https://app.example")
        message = json.loads(opener.request.data)["text"]
        self.assertEqual(result.status, "accepted")
        self.assertIn("Review", message)
        self.assertIn("2026-10-29 10:00 (UTC)", message)
        self.assertIn("https://app.example/schedules/schedule-1", message)
        self.assertNotIn("private details", message)

    @patch("app.telegram.urllib.request.build_opener")
    def test_provider_errors_are_classified_without_raw_error_text(self, build_opener):
        build_opener.return_value = FakeOpener(HTTPError("https://telegram.invalid", 429, "private body", {}, io.BytesIO(b"secret")))
        retryable = self.adapter.send_test("123")
        self.assertEqual((retryable.status, retryable.reason_code), ("retryable_failure", "provider_rate_limited"))
        build_opener.return_value = FakeOpener(HTTPError("https://telegram.invalid", 400, "private body", {}, io.BytesIO(b"secret")))
        permanent = self.adapter.send_test("123")
        self.assertEqual((permanent.status, permanent.reason_code), ("permanent_failure", "provider_rejected"))


if __name__ == "__main__":
    unittest.main()
