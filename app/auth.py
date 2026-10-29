from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import jwt

from app.config import Settings


class AuthenticationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AuthClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks = jwt.PyJWKClient(settings.auth_jwks_url, timeout=2, cache_keys=True)

    def user_id(self, authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("unauthorized")
        token = authorization[7:].strip()
        if not token or not self.settings.jwt_issuer:
            raise AuthenticationError("unauthorized")
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, signing_key, algorithms=["ES256"], issuer=self.settings.jwt_issuer, audience="notify")
            user_id = claims.get("sub")
            if not isinstance(user_id, str):
                raise AuthenticationError("unauthorized")
            response = self._get(self.settings.auth_introspect_url, token)
            if response.get("active") is not True:
                raise AuthenticationError("unauthorized")
            return user_id
        except AuthenticationError:
            raise
        except jwt.PyJWKClientConnectionError as exc:
            raise AuthenticationError("auth_unavailable") from exc
        except (jwt.PyJWTError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            if isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, "reason", None), TimeoutError):
                raise AuthenticationError("auth_unavailable") from exc
            raise AuthenticationError("unauthorized") from exc

    def account_status(self, user_id: str) -> dict[str, Any]:
        if not self.settings.internal_service_token:
            raise AuthenticationError("auth_unavailable")
        url = f"{self.settings.auth_account_status_url.rstrip('/')}/{user_id}/status"
        try:
            return self._get(url, self.settings.internal_service_token)
        except AuthenticationError as exc:
            if exc.code == "unauthorized":
                raise AuthenticationError("auth_unavailable") from exc
            raise

    def account(self, user_id: str) -> dict[str, Any]:
        status = self.account_status(user_id)
        if status.get("active") is not True or not isinstance(status.get("timezone"), str):
            raise AuthenticationError("account_inactive")
        return status

    def _get(self, url: str, bearer: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = response.read(8192)
                if response.status != 200:
                    raise AuthenticationError("auth_unavailable")
                body = json.loads(payload)
                if not isinstance(body, dict):
                    raise AuthenticationError("auth_unavailable")
                return body
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthenticationError("unauthorized") from exc
            if exc.code == 404:
                return {"active": False}
            raise AuthenticationError("auth_unavailable") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AuthenticationError("auth_unavailable") from exc
