"""Thin synchronous wrapper around Microsoft Graph v1.0 calendar APIs."""

from __future__ import annotations

import email.utils
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from core.system_timezone import SYSTEM_TIMEZONE_NAME

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
PREFER_TIMEZONE = f'outlook.timezone="{SYSTEM_TIMEZONE_NAME}"'
REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_RETRY_DELAY_SECONDS = 1.0


class GraphNotConnectedError(RuntimeError):
    """Raised when a write/client call is attempted without an access token."""


class GraphHTTPError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Microsoft Graph 请求失败（HTTP {status_code}）")


class GraphClient:
    def __init__(
        self,
        token_provider: Callable[[], str | None],
        *,
        http: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token_provider = token_provider
        self._http = http or httpx
        self._sleep = sleep

    def calendarview_delta(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        delta_link: str | None = None,
    ) -> dict[str, Any]:
        if delta_link:
            url = delta_link
        else:
            if not start or not end:
                raise ValueError("首次 calendarView delta 需要 start 和 end")
            query = urlencode({"startDateTime": start, "endDateTime": end})
            url = f"{GRAPH_BASE_URL}/me/calendarView/delta?{query}"

        events: list[dict[str, Any]] = []
        final_delta_link: str | None = None
        while url:
            payload = self._request("GET", url)
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise GraphHTTPError(502)
            events.extend(item for item in values if isinstance(item, dict))
            next_link = payload.get("@odata.nextLink")
            final_delta_link = payload.get("@odata.deltaLink") or final_delta_link
            url = str(next_link) if next_link else ""
        if not final_delta_link:
            raise GraphHTTPError(502)
        return {"events": events, "delta_link": str(final_delta_link)}

    def create_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"{GRAPH_BASE_URL}/me/events", json=dict(event))

    def update_event(self, event_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        encoded_id = quote(event_id, safe="")
        return self._request(
            "PATCH",
            f"{GRAPH_BASE_URL}/me/events/{encoded_id}",
            json=dict(changes),
        )

    def delete_event(self, event_id: str) -> None:
        encoded_id = quote(event_id, safe="")
        self._request("DELETE", f"{GRAPH_BASE_URL}/me/events/{encoded_id}", expect_json=False)

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", f"{GRAPH_BASE_URL}/me")

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        token = self._token_provider()
        if not token:
            raise GraphNotConnectedError("Microsoft 日历尚未连接")
        _validate_graph_url(url)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Prefer": PREFER_TIMEZONE,
        }
        normalized_method = method.upper()
        for attempt in range(2):
            response = self._http.request(
                normalized_method,
                url,
                headers=headers,
                json=dict(json) if json is not None else None,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            status_code = int(response.status_code)
            if 200 <= status_code < 300:
                if not expect_json or status_code == 204:
                    return {}
                payload = response.json()
                if not isinstance(payload, dict):
                    raise GraphHTTPError(502)
                return payload
            if normalized_method == "DELETE" and status_code == 404:
                return {}
            if attempt == 0 and _is_retryable(
                normalized_method,
                status_code,
                payload=json,
            ):
                self._sleep(_retry_after_seconds(response.headers))
                continue
            raise GraphHTTPError(status_code)


def _is_retryable(
    method: str,
    status_code: int,
    *,
    payload: Mapping[str, Any] | None,
) -> bool:
    if status_code == 429:
        return True
    if not 500 <= status_code <= 599:
        return False
    if method in {"GET", "HEAD", "PUT", "PATCH", "DELETE"}:
        return True
    if method != "POST" or payload is None:
        return False
    transaction_id = payload.get("transactionId")
    return isinstance(transaction_id, str) and bool(transaction_id.strip())


def _retry_after_seconds(headers: Mapping[str, str]) -> float:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return DEFAULT_RETRY_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return DEFAULT_RETRY_DELAY_SECONDS
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _validate_graph_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com":
        raise GraphHTTPError(502)
