import json
import threading
import unittest
from urllib.request import urlopen

from app.config import Settings
from app.metrics import Metrics
from app.server import build_server


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.metrics = Metrics()
        self.settings = Settings(http_host="127.0.0.1", http_port=0)
        self.server = build_server(self.settings, self.metrics)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def test_healthz(self) -> None:
        with urlopen(f"{self.base_url}/healthz", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok"})

    def test_readyz(self) -> None:
        self.metrics.mark_ready(True)

        with urlopen(f"{self.base_url}/readyz", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ready"})

    def test_metrics(self) -> None:
        self.metrics.mark_ready(True)

        with urlopen(f"{self.base_url}/metrics", timeout=5) as response:
            body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("text/plain", response.headers["Content-Type"])
        self.assertIn('app_ready{service="svc-notify"} 1', body)
        self.assertIn("app_uptime_seconds", body)

    def test_metrics_escapes_service_label(self) -> None:
        self.server.settings = Settings(
            service_name='svc-"quoted"\\name\nnext',
            http_host="127.0.0.1",
            http_port=0,
        )
        self.metrics.mark_ready(True)

        with urlopen(f"{self.base_url}/metrics", timeout=5) as response:
            body = response.read().decode("utf-8")

        self.assertIn('service="svc-\\"quoted\\"\\\\name\\nnext"', body)


if __name__ == "__main__":
    unittest.main()
