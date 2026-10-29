import json
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

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

    def test_internal_channel_capability_requires_service_bearer(self) -> None:
        self.server.settings = Settings(http_host="127.0.0.1", http_port=0, internal_service_token="secret")
        self.server.notify.internal_channels = lambda user_id: {"user_id": user_id, "channels": []}
        request = Request(f"{self.base_url}/internal/users/user-123/channels",
                          headers={"Authorization": "Bearer secret"})
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"user_id": "user-123", "channels": []})

        with self.assertRaises(Exception) as raised:
            urlopen(f"{self.base_url}/internal/users/user-123/channels", timeout=5)
        self.assertEqual(raised.exception.code, 401)

    def test_healthz_continues_incoming_trace_context(self) -> None:
        with patch("app.server.trace.get_tracer") as get_tracer, patch(
            "app.server.propagate.extract", wraps=__import__("opentelemetry").propagate.extract
        ) as extract:
            request = Request(f"{self.base_url}/healthz", headers={
                "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
            })
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)

        get_tracer.return_value.start_as_current_span.assert_called_once()
        kwargs = get_tracer.return_value.start_as_current_span.call_args.kwargs
        self.assertEqual(get_tracer.return_value.start_as_current_span.call_args.args[0], "GET /healthz")
        self.assertEqual(kwargs["attributes"]["http.route"], "/healthz")
        self.assertEqual(extract.call_args.args[0]["traceparent"], "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")

    def test_readyz(self) -> None:
        self.metrics.mark_ready(True)

        try:
            urlopen(f"{self.base_url}/readyz", timeout=5)
        except Exception as exc:
            self.assertEqual(exc.code, 503)
            body = json.loads(exc.read())
        else:
            self.fail("readiness must fail when database and NATS are unavailable")
        self.assertEqual(body, {"status": "not_ready", "database": "unavailable",
                                "events": "unavailable", "delivery": "disabled"})

    def test_metrics(self) -> None:
        self.metrics.mark_ready(True)

        with urlopen(f"{self.base_url}/metrics", timeout=5) as response:
            body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("text/plain", response.headers["Content-Type"])
        self.assertIn('app_ready{service="svc-notify"} 1', body)
        self.assertIn('notification_delivery_enabled{service="svc-notify"} 0', body)
        self.assertIn('notification_attempts_total{service="svc-notify",outcome="accepted"} 0', body)
        self.assertNotIn("reminders_sent_total", body)
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
