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
    metrics = Metrics()
    server = build_server(settings, metrics)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="http-server")

    metrics.mark_ready(True)
    logging.info("%s %s listening on :%s", settings.service_name, settings.app_version, settings.http_port)
    server_thread.start()
    try:
        shutdown_requested.wait()
    finally:
        metrics.mark_ready(False)
        server.shutdown()
        server_thread.join(timeout=15)
        server.server_close()


if __name__ == "__main__":
    main()
