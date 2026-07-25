from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from core.llm import goal_schedule_router, secondary_model


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(self.payload, ensure_ascii=False)
                    )
                )
            ]
        )


class GoalScheduleRouterTest(unittest.TestCase):
    def tearDown(self) -> None:
        secondary_model.reset()

    def test_batches_all_events_and_goals_through_secondary_model(self) -> None:
        main = FakeClient({})
        secondary = FakeClient(
            {
                "decisions": [
                    {
                        "event_id": "event-1",
                        "matches": [{"goal_id": "goal-1", "confidence": 0.92}],
                    },
                    {"event_id": "event-2", "matches": []},
                ]
            }
        )
        secondary_model.configure(secondary, "secondary-fast")

        decisions = goal_schedule_router.call_goal_schedule_router(
            main,
            "main-model",
            events=[
                {
                    "id": "event-1",
                    "subject": "科目一",
                    "series_master_id": None,
                },
                {
                    "id": "event-2",
                    "subject": "验光",
                    "series_master_id": "series-a",
                },
            ],
            goals=[
                {
                    "id": "goal-1",
                    "title": "考驾照",
                    "detail": None,
                    "horizon": "short",
                },
                {
                    "id": "goal-2",
                    "title": "跨专业考研",
                    "detail": "计算机方向",
                    "horizon": "long",
                },
            ],
        )

        self.assertEqual(
            [
                {
                    "event_id": "event-1",
                    "matches": [{"goal_id": "goal-1", "confidence": 0.92}],
                },
                {"event_id": "event-2", "matches": []},
            ],
            decisions,
        )
        self.assertEqual([], main.calls)
        self.assertEqual(1, len(secondary.calls))
        call = secondary.calls[0]
        self.assertEqual("secondary-fast", call["model"])
        self.assertEqual({"type": "json_object"}, call["response_format"])
        payload = json.loads(call["messages"][1]["content"])
        self.assertEqual(["event-1", "event-2"], [item["event_id"] for item in payload["events"]])
        self.assertEqual(["goal-1", "goal-2"], [item["goal_id"] for item in payload["active_goals"]])

    def test_unknown_goal_makes_only_that_event_decision_unusable(self) -> None:
        client = FakeClient(
            {
                "decisions": [
                    {
                        "event_id": "event-1",
                        "matches": [{"goal_id": "invented", "confidence": 0.99}],
                    },
                    {"event_id": "event-2", "matches": []},
                ]
            }
        )

        decisions = goal_schedule_router.call_goal_schedule_router(
            client,
            "main-model",
            events=[
                {"id": "event-1", "subject": "科目一", "series_master_id": None},
                {"id": "event-2", "subject": "验光", "series_master_id": None},
            ],
            goals=[
                {
                    "id": "goal-1",
                    "title": "考驾照",
                    "detail": None,
                    "horizon": "short",
                }
            ],
        )

        self.assertEqual([{"event_id": "event-2", "matches": []}], decisions)


if __name__ == "__main__":
    unittest.main()
