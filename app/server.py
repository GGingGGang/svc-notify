from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.parse import unquote
import hmac

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.config import Settings
from app.metrics import Metrics
from app.service import NotifyService, ServiceError

logger = logging.getLogger(__name__)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings: Settings, metrics: Metrics, service: NotifyService | None = None) -> None:
        self.settings = settings
        self.metrics = metrics
        self.notify = service or NotifyService(settings)
        self.nats_worker = None
        super().__init__((settings.http_host, settings.http_port), RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: AppServer

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin not in _allowed_origins(self.server.settings):
            self._json(HTTPStatus.FORBIDDEN, {"error": "origin_not_allowed"})
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors(origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _dispatch(self) -> None:
        path = _route_path(self.path)
        route = _route_template(path)
        context = propagate.extract({"traceparent": self.headers.get("traceparent", ""), "baggage": self.headers.get("baggage", "")})
        tracer = trace.get_tracer("svc-notify")
        with tracer.start_as_current_span(
            f"{self.command} {route}", context=context, kind=SpanKind.SERVER,
            attributes={"http.request.method": self.command, "http.route": route},
        ) as span:
            try:
                self._handle(path)
                status = getattr(self, "response_status", HTTPStatus.NOT_FOUND)
                span.set_attribute("http.response.status_code", int(status))
                if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                    span.set_status(Status(StatusCode.ERROR))
            except ServiceError as exc:
                self._json(exc.status, {"error": exc.code})
                span.set_attribute("http.response.status_code", exc.status)
            except (ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                span.set_attribute("http.response.status_code", HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                logger.warning("request failed (%s)", type(exc).__name__)
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "service_unavailable"})
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("http.response.status_code", HTTPStatus.SERVICE_UNAVAILABLE)

    def _handle(self, path: str) -> None:
        method = self.command
        if method == "GET" and path.startswith("/internal/users/") and path.endswith("/channels"):
            token = self.server.settings.internal_service_token
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}" if token else ""
            if not token or not hmac.compare_digest(supplied, expected):
                raise ServiceError(401, "unauthorized")
            user_id = unquote(path[len("/internal/users/"):-len("/channels")].strip("/"))
            self._json(HTTPStatus.OK, self.server.notify.internal_channels(user_id))
            return
        if method == "GET" and path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if method == "GET" and path == "/readyz":
            database_ready = _database_ready(self.server.notify)
            events_ready = bool(self.server.nats_worker and self.server.nats_worker.connected)
            ready = database_ready and events_ready
            self._json(HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE, {
                "status": "ready" if ready else "not_ready",
                "database": "ready" if database_ready else "unavailable",
                "events": "ready" if events_ready else "unavailable",
                "delivery": "enabled" if self.server.notify.telegram_available else "disabled",
            })
            return
        if method == "GET" and path == "/metrics":
            self._text(HTTPStatus.OK, self.server.metrics.render_prometheus(self.server.settings.service_name,
                       self.server.notify.telegram_available), "text/plain; version=0.0.4; charset=utf-8")
            return
        if method == "GET" and path == "/capabilities":
            self._json(HTTPStatus.OK, self.server.notify.capabilities())
            return
        if method == "GET" and path == "/webhooks/telegram":
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
            return
        if method == "POST" and path == "/webhooks/telegram":
            body = self._body()
            accepted = self.server.notify.telegram_update(self.headers.get("X-Telegram-Bot-Api-Secret-Token"), body)
            self._json(HTTPStatus.OK, {"accepted": accepted})
            return

        user_id = self.server.notify.user_id(self.headers.get("Authorization"))
        if method == "GET" and path == "/channels":
            self._json(HTTPStatus.OK, self.server.notify.channels(user_id))
        elif method == "POST" and path == "/channels/telegram/connect":
            self._json(HTTPStatus.CREATED, self.server.notify.connect_telegram(user_id))
        elif method == "GET" and path == "/preferences/notifications":
            self._json(HTTPStatus.OK, self.server.notify.preferences(user_id))
        elif method == "PUT" and path == "/preferences/notifications":
            self._json(HTTPStatus.OK, self.server.notify.set_preferences(user_id, self._body()))
        elif method == "POST" and path == "/notifications/test":
            body = self._body()
            if set(body) != {"channel_id"} or not isinstance(body["channel_id"], str):
                raise ServiceError(400, "invalid_request")
            result = self.server.notify.request_test(user_id, body["channel_id"], self.headers.get("Idempotency-Key", ""))
            self._json(HTTPStatus.ACCEPTED, result)
        elif method == "GET" and path == "/notifications":
            self._json(HTTPStatus.OK, self.server.notify.history(user_id, parse_qs(urlsplit(self.path).query)))
        elif path.startswith("/channels/") and path.endswith("/consent") and method == "PATCH":
            channel_id = path[len("/channels/"):-len("/consent")].strip("/")
            body = self._body()
            if set(body) != {"enabled"}:
                raise ServiceError(400, "invalid_request")
            self._json(HTTPStatus.OK, self.server.notify.consent(user_id, channel_id, body["enabled"]))
        elif path.startswith("/channels/") and method == "DELETE":
            channel_id = path[len("/channels/"):].strip("/")
            if not channel_id or "/" in channel_id:
                raise ServiceError(404, "not_found")
            self.server.notify.disconnect(user_id, channel_id)
            self.send_response(HTTPStatus.NO_CONTENT)
            origin = self.headers.get("Origin", "")
            if origin in _allowed_origins(self.server.settings):
                self._cors(origin)
            self.end_headers()
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > 32768:
            raise ServiceError(413, "request_too_large")
        raw = self.rfile.read(length)
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        return body

    def _cors(self, origin: str) -> None:
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _json(self, status: HTTPStatus, body: dict) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.response_status = status
        if "Origin" in self.headers and self.headers.get("Origin") in _allowed_origins(self.server.settings):
            self._cors(self.headers["Origin"])
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


def _route_path(raw: str) -> str:
    path = urlsplit(raw).path
    prefix = "/v1/notify"
    return path[len(prefix):] or "/" if path == prefix or path.startswith(prefix + "/") else path


def _route_template(path: str) -> str:
    if path.startswith("/internal/users/") and path.endswith("/channels"):
        return "/internal/users/{user_id}/channels"
    if path.startswith("/channels/"):
        return "/channels/{channel_id}/consent" if path.endswith("/consent") else "/channels/{channel_id}"
    return path if path in {"/healthz", "/readyz", "/metrics", "/capabilities", "/channels",
                           "/channels/telegram/connect", "/preferences/notifications", "/notifications",
                           "/notifications/test", "/webhooks/telegram"} else "/{unmatched}"


def _allowed_origins(settings: Settings) -> set[str]:
    return {origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()}


def _database_ready(service: NotifyService) -> bool:
    try:
        return bool(service.store and service.store.available and service.store.ping())
    except Exception:
        return False


def build_server(settings: Settings, metrics: Metrics, service: NotifyService | None = None) -> AppServer:
    return AppServer(settings, metrics, service)
