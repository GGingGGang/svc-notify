from __future__ import annotations

import json
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.config import Settings


class BatchUnavailable(Exception):
    pass


class BatchClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def reminder_validity(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.internal_service_token:
            raise BatchUnavailable("batch_unavailable")
        dispatch_id = urllib.parse.quote(str(event["dispatch_id"]), safe="")
        query = urllib.parse.urlencode({"attempt": event["attempt"]})
        url = (f"{self.settings.batch_url.rstrip('/')}/internal/reminders/{dispatch_id}/validity?{query}")
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.settings.internal_service_token}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                body = json.loads(response.read(8192))
                if response.status != 200 or not isinstance(body, dict) or not isinstance(body.get("valid"), bool):
                    raise BatchUnavailable("batch_unavailable")
                if body["valid"] and not _matches_due(body, event):
                    raise BatchUnavailable("batch_contract_mismatch")
                return body
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"valid": False}
            raise BatchUnavailable("batch_unavailable") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            raise BatchUnavailable("batch_unavailable") from exc


def _matches_due(snapshot: dict[str, Any], event: dict[str, Any]) -> bool:
    matches = (
        snapshot.get("dispatch_id", event["dispatch_id"]) == event["dispatch_id"]
        and snapshot.get("user_id") == event["user_id"]
        and snapshot.get("schedule_id") == event["schedule_id"]
        and snapshot.get("minutes_before") == event["minutes_before"]
        and snapshot.get("channel") == event["channel"]
        and isinstance(snapshot.get("revision"), int)
        and isinstance(snapshot.get("title"), str)
        and bool(snapshot["title"])
        and isinstance(snapshot.get("start_at"), str)
        and isinstance(snapshot.get("remind_at"), str)
    )
    if not matches:
        return False
    try:
        _instant(snapshot["start_at"])
        _instant(snapshot["remind_at"])
    except ValueError:
        return False
    return True


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)
