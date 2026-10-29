from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from datetime import datetime, timezone

from app.config import Settings
from app.config import load_settings
from app.metrics import Metrics
from app.server import build_server
from app.service import NotifyService
from app.store import Store
from app.tracing import configure_tracing, shutdown_tracing
from app.nats_worker import NatsWorker


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "msg": record.getMessage(),
            "service": "notify",
        })


def main() -> None:
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger().handlers[0].setFormatter(JsonFormatter())
    settings = load_settings()
    shutdown_requested = threading.Event()

    def request_shutdown(signum: int, _frame: object) -> None:
        logging.info("received signal %s, shutting down", signum)
        shutdown_requested.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    run_service(settings, shutdown_requested)


def run_service(settings: Settings, shutdown_requested: threading.Event) -> None:
    tracer_provider = configure_tracing(settings.service_name, settings.app_version)
    metrics = Metrics()
    store = None
    if settings.mysql_host and settings.mysql_user and settings.mysql_password and settings.encryption_key:
        try:
            store = Store(settings)
            store.ensure_schema()
        except Exception as exc:
            logging.warning("notification storage unavailable (%s)", type(exc).__name__)
    service = NotifyService(settings, store=store)
    service.metrics = metrics
    worker = NatsWorker(settings, service, store) if store else None
    if worker:
        worker.start()
    cleanup_thread = threading.Thread(target=_run_retention, args=(shutdown_requested, store),
                                      name="notify-retention", daemon=True)
    cleanup_thread.start()
    server = build_server(settings, metrics, service)
    server.nats_worker = worker
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="http-server")

    metrics.mark_ready(True)
    logging.info("%s %s listening on :%s", settings.service_name, settings.app_version, settings.http_port)
    server_thread.start()
    try:
        shutdown_requested.wait()
    finally:
        metrics.mark_ready(False)
        if worker:
            worker.stop()
        cleanup_thread.join(timeout=2)
        server.shutdown()
        server_thread.join(timeout=15)
        server.server_close()
        shutdown_tracing(tracer_provider)


def _run_retention(shutdown_requested: threading.Event, store: Store | None) -> None:
    while not shutdown_requested.wait(3600):
        if not store or not store.available or not store.schema_ready:
            continue
        try:
            store.cleanup()
        except Exception as exc:
            logging.warning("notification retention cleanup failed (%s)", type(exc).__name__)



if __name__ == "__main__":
    main()
