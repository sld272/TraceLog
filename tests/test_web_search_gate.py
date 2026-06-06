from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from core import web_search_gate


class FakeClient:
    def __init__(self, payload: dict | str) -> None:
        self.payload = payload
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class WebSearchGateTest(unittest.TestCase):
    def test_parse_valid_search_decision_limits_and_dedupes_queries(self) -> None:
        parsed = web_search_gate.parse_decision_payload(
            json.dumps(
                {
                    "should_search": True,
                    "queries": [" Python 3.13 release ", "Python 3.13 release", "OpenAI latest", "extra"],
                    "reason": "当前事实",
                    "freshness_required": True,
                },
                ensure_ascii=False,
            )
        )

        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["should_search"])
        self.assertEqual(["Python 3.13 release", "OpenAI latest", "extra"], parsed["queries"])
        self.assertTrue(parsed["freshness_required"])

    def test_parse_search_without_queries_degrades_to_no_search(self) -> None:
        parsed = web_search_gate.parse_decision_payload('{"should_search": true, "queries": []}')

        self.assertIsNotNone(parsed)
        self.assertFalse(parsed["should_search"])
        self.assertEqual([], parsed["queries"])

    def test_parse_filters_obviously_private_queries(self) -> None:
        parsed = web_search_gate.parse_decision_payload(
            json.dumps(
                {
                    "should_search": True,
                    "queries": [
                        "用户 13800138000 最近在哪",
                        "OpenAI latest model",
                        "alice@example.com profile",
                        "sk-abcdefghijklmnopqrstuvwxyz123456",
                    ],
                    "reason": "混合搜索词",
                    "freshness_required": True,
                },
                ensure_ascii=False,
            )
        )

        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["should_search"])
        self.assertEqual(["OpenAI latest model"], parsed["queries"])

    def test_decide_defaults_to_no_search_on_invalid_json(self) -> None:
        client = FakeClient("not json")

        decision = web_search_gate.decide(client, "fake-model", "查一下今天新闻", channel="chat")

        self.assertFalse(decision.should_search)
        self.assertEqual("gate_failed", decision.reason)

    def test_decide_returns_llm_decision(self) -> None:
        client = FakeClient(
            {
                "should_search": True,
                "queries": ["OpenAI latest model"],
                "reason": "用户询问当前公开事实",
                "freshness_required": True,
            }
        )

        decision = web_search_gate.decide(client, "fake-model", "今天 OpenAI 最新模型是什么", channel="chat")

        self.assertTrue(decision.should_search)
        self.assertEqual(["OpenAI latest model"], decision.queries)
        self.assertTrue(decision.freshness_required)
        self.assertIn("网页搜索判断器", client.calls[0]["messages"][0]["content"])

    def test_decide_prompt_encourages_light_search_for_named_public_works(self) -> None:
        client = FakeClient(
            {
                "should_search": True,
                "queries": ["在超市后门吸烟的二人 动画"],
                "reason": "用户提到具名公开作品，轻量搜索背景有助于贴切回应。",
                "freshness_required": False,
            }
        )

        decision = web_search_gate.decide(
            client,
            "fake-model",
            "刚刚看了新番《在超市后门吸烟的二人》，好好磕啊",
            channel="public_post",
        )
        prompt = client.calls[0]["messages"][0]["content"]

        self.assertTrue(decision.should_search)
        self.assertEqual(["在超市后门吸烟的二人 动画"], decision.queries)
        self.assertFalse(decision.freshness_required)
        self.assertIn("具名公开作品", prompt)
        self.assertIn("轻量背景搜索", prompt)
        self.assertIn("不要把用户整句情绪表达放进搜索词", prompt)


if __name__ == "__main__":
    unittest.main()
