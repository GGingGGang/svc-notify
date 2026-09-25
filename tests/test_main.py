import json
import logging
import threading
import unittest

from app.config import Settings
from app.main import JsonFormatter
from app.main import run_service


class MainLifecycleTest(unittest.TestCase):
    def test_log_is_one_json_line(self) -> None:
        record = logging.LogRecord("notify", logging.INFO, "", 0, "ready %s", ('secret"\nvalue',), None)
        entry = JsonFormatter().format(record)
        self.assertNotIn("\n", entry)
        self.assertEqual(json.loads(entry)["msg"], 'ready secret"\nvalue')
        self.assertEqual(json.loads(entry)["service"], "notify")

    def test_run_service_exits_after_shutdown_request(self) -> None:
        shutdown_requested = threading.Event()
        settings = Settings(http_host="127.0.0.1", http_port=0)
        thread = threading.Thread(target=run_service, args=(settings, shutdown_requested))

        thread.start()
        shutdown_requested.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
