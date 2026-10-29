import io
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

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


if __name__ == "__main__":
    unittest.main()
