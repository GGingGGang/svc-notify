from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.config import Settings
from app.metrics import Metrics


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings: Settings, metrics: Metrics) -> None:
        self.settings = settings
        self.metrics = metrics
        super().__init__((settings.http_host, settings.http_port), RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: AppServer

    def do_GET(self) -> None:
        route = self.path if self.path in {"/healthz", "/readyz", "/metrics"} else "/{unmatched}"
        context = propagate.extract({"traceparent": self.headers.get("traceparent", ""), "baggage": self.headers.get("baggage", "")})
        tracer = trace.get_tracer("svc-notify")
        with tracer.start_as_current_span(
            f"GET {route}", context=context, kind=SpanKind.SERVER,
            attributes={"http.request.method": "GET", "http.route": route},
        ) as span:
            try:
                self._handle_get()
                status = getattr(self, "response_status", HTTPStatus.NOT_FOUND)
                span.set_attribute("http.response.status_code", int(status))
                if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                    span.set_status(Status(StatusCode.ERROR))
            except Exception:
                span.set_status(Status(StatusCode.ERROR))
                raise

    def _handle_get(self) -> None:
        if self.path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/readyz":
            if self.server.metrics.ready:
                self._json(HTTPStatus.OK, {"status": "ready", "delivery": "disabled"})
            else:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "not_ready", "delivery": "disabled"})
            return
        if self.path == "/metrics":
            self._text(
                HTTPStatus.OK,
                self.server.metrics.render_prometheus(self.server.settings.service_name),
                "text/plain; version=0.0.4; charset=utf-8",
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _json(self, status: HTTPStatus, body: dict[str, str]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.response_status = status
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _text(self, status: HTTPStatus, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.response_status = status
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def build_server(settings: Settings, metrics: Metrics) -> AppServer:
    return AppServer(settings, metrics)
