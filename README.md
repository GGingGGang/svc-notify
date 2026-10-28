# svc-notify

The service currently provides HTTP health checks, readiness, Prometheus metrics, and optional OpenTelemetry tracing. Notification delivery is disabled: `/readyz` reports `"delivery":"disabled"`, and the service does not send email, push messages, or schedule reminders. A ready response means the HTTP process is ready only.

Run locally with Python 3.13 or newer:

```bash
python -m pip install -e .
python -m app.main
```

The service listens on `0.0.0.0:8080` by default. Set `HTTP_HOST`, `HTTP_PORT`, `SERVICE_NAME`, or `APP_VERSION` to override those values. `GET /healthz` is the liveness check, `GET /readyz` reports HTTP readiness and disabled delivery, and `GET /metrics` exposes Prometheus metrics.

Tracing is off by default. To enable the OTLP gRPC exporter, set `OTEL_TRACES_EXPORTER=otlp` and configure `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4317`).

Run the unit tests with:

```bash
python -m unittest discover -s tests
```
