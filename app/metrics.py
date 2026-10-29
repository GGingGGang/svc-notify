from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

_OUTCOMES = ("accepted", "retryable_failure", "permanent_failure", "unknown", "skipped", "cancelled")


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.time)
    ready: bool = False
    attempt_counts: dict[str, int] = field(default_factory=lambda: {name: 0 for name in _OUTCOMES})
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def mark_ready(self, value: bool) -> None:
        with self._lock:
            self.ready = value

    def record_attempt_completion(self, outcome: str) -> None:
        if outcome not in self.attempt_counts:
            return
        with self._lock:
            self.attempt_counts[outcome] += 1

    def render_prometheus(self, service_name: str, delivery_enabled: bool = False) -> str:
        with self._lock:
            uptime = max(0.0, time.time() - self.started_at)
            ready = 1 if self.ready else 0
            counts = dict(self.attempt_counts)

        labels = f'service="{_escape_label_value(service_name)}"'
        lines = [
            "# HELP app_uptime_seconds Seconds since the service process started.",
            "# TYPE app_uptime_seconds gauge",
            f"app_uptime_seconds{{{labels}}} {uptime:.3f}",
            "# HELP app_ready Readiness state of the HTTP service.",
            "# TYPE app_ready gauge",
            f"app_ready{{{labels}}} {ready}",
            "# HELP notification_delivery_enabled Whether actual notification delivery is configured.",
            "# TYPE notification_delivery_enabled gauge",
            f"notification_delivery_enabled{{{labels}}} {1 if delivery_enabled else 0}",
        ]
        lines.extend(("# HELP notification_attempts_total First durable completion per reminder attempt outcome.",
                      "# TYPE notification_attempts_total counter"))
        for outcome, count in counts.items():
            lines.append(f'notification_attempts_total{{{labels},outcome="{outcome}"}} {count}')
        lines.append("")
        return "\n".join(lines)


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
