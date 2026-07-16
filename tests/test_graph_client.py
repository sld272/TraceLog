from __future__ import annotations

import unittest

from core.graph.client import GraphClient, GraphHTTPError, PREFER_TIMEZONE


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None) -> None:
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.headers = {} if headers is None else headers

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


class GraphClientTest(unittest.TestCase):
    def test_initial_delta_follows_pages_and_returns_delta_link(self) -> None:
        http = FakeHttp(
            [
                FakeResponse(
                    200,
                    {
                        "value": [{"id": "e1"}],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
                    },
                ),
                FakeResponse(
                    200,
                    {
                        "value": [{"id": "e2"}],
                        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-1",
                    },
                ),
            ]
        )
        client = GraphClient(lambda: "secret-token", http=http)

        result = client.calendarview_delta(
            start="2026-01-01T00:00:00+08:00",
            end="2026-02-01T00:00:00+08:00",
        )

        self.assertEqual(["e1", "e2"], [event["id"] for event in result["events"]])
        self.assertEqual("https://graph.microsoft.com/v1.0/delta-1", result["delta_link"])
        self.assertIn("startDateTime=", http.calls[0]["url"])
        self.assertEqual(PREFER_TIMEZONE, http.calls[0]["headers"]["Prefer"])
        self.assertEqual(15.0, http.calls[0]["timeout"])
        self.assertEqual("Bearer secret-token", http.calls[0]["headers"]["Authorization"])

    def test_incremental_delta_uses_link_without_window_parameters(self) -> None:
        http = FakeHttp(
            [
                FakeResponse(
                    200,
                    {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/delta-2"},
                )
            ]
        )
        client = GraphClient(lambda: "token", http=http)

        client.calendarview_delta(delta_link="https://graph.microsoft.com/delta-1")

        self.assertEqual("https://graph.microsoft.com/delta-1", http.calls[0]["url"])

    def test_429_retries_once_and_respects_retry_after(self) -> None:
        delays: list[float] = []
        http = FakeHttp(
            [
                FakeResponse(429, headers={"Retry-After": "2"}),
                FakeResponse(200, {"id": "me"}),
            ]
        )
        client = GraphClient(lambda: "token", http=http, sleep=delays.append)

        result = client.get_me()

        self.assertEqual("me", result["id"])
        self.assertEqual([2.0], delays)
        self.assertEqual(2, len(http.calls))

    def test_5xx_retries_once(self) -> None:
        http = FakeHttp([FakeResponse(503), FakeResponse(200, {"id": "me"})])
        client = GraphClient(lambda: "token", http=http, sleep=lambda delay: None)

        result = client.get_me()

        self.assertEqual("me", result["id"])
        self.assertEqual(2, len(http.calls))

    def test_410_is_exposed_to_schedule_service(self) -> None:
        http = FakeHttp([FakeResponse(410)])
        client = GraphClient(lambda: "token", http=http)

        with self.assertRaises(GraphHTTPError) as raised:
            client.calendarview_delta(delta_link="https://graph.microsoft.com/expired")

        self.assertEqual(410, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
