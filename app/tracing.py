from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.baggage import W3CBaggagePropagator
from opentelemetry.propagators.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)


def configure_tracing(service_name: str, app_version: str) -> TracerProvider | None:
    propagator = CompositePropagator([
        TraceContextTextMapPropagator(),
        W3CBaggagePropagator(),
    ])
    propagate.set_global_textmap(propagator)

    if os.environ.get("OTEL_TRACES_EXPORTER", "none").lower() != "otlp":
        return None

    attributes = {"service.namespace": service_name, "service.version": app_version}
    for pair in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, separator, value = pair.partition("=")
        if separator and key.strip():
            attributes[key.strip()] = value.strip()
    try:
        resource = Resource.create({
            **attributes,
            "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
        })
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        endpoint = endpoint or "http://localhost:4317"
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=urlsplit(endpoint).scheme == "http")
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        return provider
    except Exception as exc:
        logger.warning("OpenTelemetry tracing initialization failed (%s); tracing is disabled", type(exc).__name__)
        return None


def shutdown_tracing(provider: TracerProvider | None) -> None:
    if provider is None:
        return
    try:
        provider.force_flush(timeout_millis=2000)
        provider.shutdown()
    except Exception:
        logger.warning("OpenTelemetry tracing shutdown failed; tracing flush is incomplete")
