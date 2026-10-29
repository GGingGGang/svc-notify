import unittest
from unittest.mock import Mock

from app.auth import AuthClient, AuthenticationError
from app.config import Settings


class AuthClientTest(unittest.TestCase):
    def test_internal_token_rejection_is_unavailable_not_inactive_account(self):
        client = AuthClient(Settings(internal_service_token="wrong"))
        client._get = Mock(side_effect=AuthenticationError("unauthorized"))
        with self.assertRaisesRegex(AuthenticationError, "auth_unavailable"):
            client.account("user-id")

    def test_missing_internal_token_fails_closed(self):
        client = AuthClient(Settings())
        with self.assertRaisesRegex(AuthenticationError, "auth_unavailable"):
            client.account("user-id")


if __name__ == "__main__":
    unittest.main()
