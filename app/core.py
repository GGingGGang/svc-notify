from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.config import Settings


class CoreUnavailable(Exception):
    pass


class CoreClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def reminder_validity(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.internal_service_token:
            raise CoreUnavailable("core_unavailable")
        query = urllib.parse.urlencode({
            "user_id": event["user_id"],
            "revision": event["schedule_revision"],
            "minutes_before": event["minutes_before"],
            "channel": event["channel"],
        })
        schedule_id = urllib.parse.quote(str(event["schedule_id"]), safe="")
        url = f"{self.settings.core_url.rstrip('/')}/internal/reminders/{schedule_id}/validity?{query}"
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.settings.internal_service_token}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                body = json.loads(response.read(8192))
                if response.status != 200 or not isinstance(body, dict) or not isinstance(body.get("valid"), bool):
                    raise CoreUnavailable("core_unavailable")
                return body
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404}:
                return {"valid": False}
            raise CoreUnavailable("core_unavailable") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            raise CoreUnavailable("core_unavailable") from exc
