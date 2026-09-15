import unittest

from app.config import load_settings


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = load_settings({})

        self.assertEqual(settings.service_name, "svc-notify")
        self.assertEqual(settings.http_host, "0.0.0.0")
        self.assertEqual(settings.http_port, 8080)

    def test_env_overrides(self) -> None:
        settings = load_settings(
            {
                "SERVICE_NAME": "svc-alerts",
                "HTTP_HOST": "127.0.0.1",
                "HTTP_PORT": "9090",
                "APP_VERSION": "abc123",
            }
        )

        self.assertEqual(settings.service_name, "svc-alerts")
        self.assertEqual(settings.http_host, "127.0.0.1")
        self.assertEqual(settings.http_port, 9090)
        self.assertEqual(settings.app_version, "abc123")

    def test_invalid_port_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP_PORT"):
            load_settings({"HTTP_PORT": "nope"})

    def test_out_of_range_port_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP_PORT"):
            load_settings({"HTTP_PORT": "65536"})


if __name__ == "__main__":
    unittest.main()
