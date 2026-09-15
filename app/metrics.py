from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.time)
    ready: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def mark_ready(self, value: bool) -> None:
        with self._lock:
            self.ready = value

    def render_prometheus(self, service_name: str) -> str:
        with self._lock:
            uptime = max(0.0, time.time() - self.started_at)
            ready = 1 if self.ready else 0

        labels = f'service="{_escape_label_value(service_name)}"'
        lines = [
            "# HELP app_uptime_seconds Seconds since the service process started.",
            "# TYPE app_uptime_seconds gauge",
            f"app_uptime_seconds{{{labels}}} {uptime:.3f}",
            "# HELP app_ready Readiness state of the HTTP service.",
            "# TYPE app_ready gauge",
            f"app_ready{{{labels}}} {ready}",
            "",
        ]
        return "\n".join(lines)


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
