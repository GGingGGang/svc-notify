import os
import unittest
from unittest.mock import Mock, patch

from app.tracing import configure_tracing, shutdown_tracing


class TracingTest(unittest.TestCase):
    def test_disabled_tracing_returns_without_creating_provider(self) -> None:
        with patch.dict(os.environ, {"OTEL_TRACES_EXPORTER": "none"}), patch(
            "app.tracing.TracerProvider"
        ) as provider_type:
            self.assertIsNone(configure_tracing("svc-notify", "test"))

        provider_type.assert_not_called()

    def test_enabled_tracing_initializes_and_shuts_down_provider(self) -> None:
        provider = Mock()
        with patch.dict(os.environ, {"OTEL_TRACES_EXPORTER": "otlp"}), patch(
            "app.tracing.OTLPSpanExporter"
        ) as exporter_type, patch("app.tracing.BatchSpanProcessor"), patch(
            "app.tracing.TracerProvider", return_value=provider
        ), patch("app.tracing.trace.set_tracer_provider") as set_provider:
            configured = configure_tracing("svc-notify", "test")

        self.assertIs(configured, provider)
        exporter_type.assert_called_once()
        set_provider.assert_called_once_with(provider)
        shutdown_tracing(configured)
        provider.force_flush.assert_called_once_with(timeout_millis=2000)
        provider.shutdown.assert_called_once_with()

    def test_exporter_initialization_failure_disables_tracing(self) -> None:
        with patch.dict(os.environ, {"OTEL_TRACES_EXPORTER": "otlp"}), patch(
            "app.tracing.OTLPSpanExporter", side_effect=RuntimeError("unavailable")
        ):
            self.assertIsNone(configure_tracing("svc-notify", "test"))

    def test_shutdown_failure_is_contained(self) -> None:
        provider = Mock()
        provider.shutdown.side_effect = RuntimeError("unavailable")

        shutdown_tracing(provider)

        provider.force_flush.assert_called_once_with(timeout_millis=2000)
        provider.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
