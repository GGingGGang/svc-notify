import threading
import unittest

from app.config import Settings
from app.main import run_service


class MainLifecycleTest(unittest.TestCase):
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
