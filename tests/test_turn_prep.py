from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import turn_prep, web_search_service
from core.llm import secondary_model, turn_prep_router


class FakeClient:
    def __init__(self, payload: dict | str) -> None:
        self.payload = payload
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _config(enabled: bool) -> web_search_service.WebSearchConfig:
    return web_search_service.WebSearchConfig(
        enabled=enabled,
        provider="duckduckgo",
        tavily_api_key=None,
        max_results=5,
        timeout_s=8,
        cache_ttl_s=0,
    )


def _search_enabled():
    return patch("core.turn_prep.web_search_service.effective_config", return_value=_config(True))


def _search_disabled():
    return patch("core.turn_prep.web_search_service.effective_config", return_value=_config(False))


MERGED_FULL = {
    "should_search": True,
    "queries": ["Python 3.13 release"],
    "reason": "当前版本事实",
    "freshness_required": True,
    "semantic_query": "用户询问 Python 3.13 稳定版发布信息",
    "keywords": ["Python", "3.13", "稳定版"],
}


class TurnPrepTest(unittest.TestCase):
    def tearDown(self) -> None:
        secondary_model.reset()

    def test_merged_call_routes_to_secondary_model(self) -> None:
        main = FakeClient(MERGED_FULL)
        secondary = FakeClient(MERGED_FULL)
        secondary_model.configure(secondary, "fast-mini")

        with _search_enabled():
            turn_prep.prepare_turn(main, "main-model", user_message="今天 Python 3.13 发布了吗", channel="chat")

        self.assertEqual([], main.calls)
        self.assertEqual(1, len(secondary.calls))
        self.assertEqual("fast-mini", secondary.calls[0]["model"])

    def test_full_json_applies_both_halves_in_one_call(self) -> None:
        client = FakeClient(MERGED_FULL)

        with _search_enabled():
            prep = turn_prep.prepare_turn(client, "m", user_message="今天 Python 3.13 发布了吗", channel="chat")

        # Gate + rewrite share a SINGLE merged LLM call.
        self.assertEqual(1, len(client.calls))
        self.assertTrue(prep.search_decision.should_search)
        self.assertEqual(["Python 3.13 release"], prep.search_decision.queries)
        self.assertTrue(prep.search_decision.freshness_required)
        self.assertTrue(prep.rewritten.used_rewrite)
        self.assertEqual("用户询问 Python 3.13 稳定版发布信息", prep.rewritten.semantic_query)
        self.assertIn("Python", prep.rewritten.keywords)

    def test_bad_json_falls_back_on_both_halves(self) -> None:
        client = FakeClient("这根本不是 JSON")

        with _search_enabled():
            prep = turn_prep.prepare_turn(client, "m", user_message="随便聊聊", channel="chat")

        self.assertEqual(1, len(client.calls))
        self.assertFalse(prep.search_decision.should_search)
        self.assertEqual("turn_prep_failed", prep.search_decision.reason)
        self.assertFalse(prep.rewritten.used_rewrite)
        self.assertEqual("随便聊聊", prep.rewritten.semantic_query)

    def test_half_valid_search_ok_rewrite_falls_back(self) -> None:
        payload = {
            "should_search": True,
            "queries": ["OpenAI latest model"],
            "reason": "事实",
            "freshness_required": False,
            "semantic_query": "",   # rewrite half invalid
            "keywords": [],
        }
        client = FakeClient(payload)

        with _search_enabled():
            prep = turn_prep.prepare_turn(client, "m", user_message="OpenAI 最新模型", channel="chat")

        self.assertTrue(prep.search_decision.should_search)
        self.assertEqual(["OpenAI latest model"], prep.search_decision.queries)
        self.assertFalse(prep.rewritten.used_rewrite)
        self.assertEqual("OpenAI 最新模型", prep.rewritten.semantic_query)

    def test_half_valid_rewrite_ok_search_says_no(self) -> None:
        payload = {
            "should_search": False,
            "queries": [],
            "reason": "闲聊",
            "freshness_required": False,
            "semantic_query": "用户在回忆图书馆学习偏好",
            "keywords": ["图书馆", "学习"],
        }
        client = FakeClient(payload)

        with _search_enabled():
            prep = turn_prep.prepare_turn(client, "m", user_message="我之前说过图书馆学习效率高吧", channel="chat")

        self.assertFalse(prep.search_decision.should_search)
        self.assertTrue(prep.rewritten.used_rewrite)
        self.assertEqual(["图书馆", "学习"], prep.rewritten.keywords)

    def test_search_disabled_runs_rewrite_only_no_merged_call(self) -> None:
        client = FakeClient({"semantic_query": "用户在回忆图书馆学习偏好", "keywords": ["图书馆"]})

        with _search_disabled(), patch("core.llm.turn_prep_router.call_turn_prep") as merged:
            prep = turn_prep.prepare_turn(client, "m", user_message="我之前说过图书馆学习效率高吧", channel="chat")

        merged.assert_not_called()          # no merged call when search is off
        self.assertEqual(1, len(client.calls))  # only the single rewrite call
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn("query rewrite 引擎", prompt)   # the rewrite prompt, not turn-prep
        self.assertNotIn("回合预处理器", prompt)
        self.assertFalse(prep.search_decision.should_search)
        self.assertEqual("disabled", prep.search_decision.reason)
        self.assertTrue(prep.rewritten.used_rewrite)

    def test_all_private_queries_degrade_to_no_search(self) -> None:
        payload = {
            "should_search": True,
            "queries": ["用户 13800138000 最近在哪", "alice@example.com profile"],
            "reason": "混合搜索词",
            "freshness_required": True,
            "semantic_query": "定位用户",
            "keywords": [],
        }
        client = FakeClient(payload)

        with _search_enabled():
            prep = turn_prep.prepare_turn(client, "m", user_message="查一下这个人在哪", channel="chat")

        self.assertFalse(prep.search_decision.should_search)
        self.assertEqual([], prep.search_decision.queries)

    def test_private_query_filtered_but_public_survives(self) -> None:
        payload = {
            "should_search": True,
            "queries": ["用户 13800138000 最近在哪", "OpenAI latest model"],
            "reason": "混合搜索词",
            "freshness_required": True,
            "semantic_query": "x",
            "keywords": [],
        }
        client = FakeClient(payload)

        with _search_enabled():
            prep = turn_prep.prepare_turn(client, "m", user_message="查一下", channel="chat")

        self.assertTrue(prep.search_decision.should_search)
        self.assertEqual(["OpenAI latest model"], prep.search_decision.queries)

    def test_empty_and_slash_command_skip_both_without_any_call(self) -> None:
        for message in ["", "   ", "/quit"]:
            with self.subTest(message=message):
                client = FakeClient(MERGED_FULL)
                with _search_enabled(), patch("core.llm.turn_prep_router.call_turn_prep") as merged:
                    prep = turn_prep.prepare_turn(client, "m", user_message=message, channel="chat")

                merged.assert_not_called()
                self.assertEqual([], client.calls)
                self.assertFalse(prep.search_decision.should_search)
                self.assertFalse(prep.rewritten.used_rewrite)
                self.assertTrue(prep.rewritten.rewrite_skipped_by_gate)

    def test_no_client_falls_back_without_call(self) -> None:
        with _search_enabled(), patch("core.llm.turn_prep_router.call_turn_prep") as merged:
            prep = turn_prep.prepare_turn(None, None, user_message="今天新闻", channel="chat")

        merged.assert_not_called()
        self.assertFalse(prep.search_decision.should_search)
        self.assertEqual("missing_llm_client", prep.search_decision.reason)
        self.assertFalse(prep.rewritten.used_rewrite)
        self.assertEqual("今天新闻", prep.rewritten.semantic_query)

    def test_prompt_merges_both_duties_verbatim(self) -> None:
        prompt = turn_prep_router.TURN_PREP_PROMPT

        # Search-gate half kept its criteria and privacy/light-search rules.
        self.assertIn("网页搜索判断", prompt)
        self.assertIn("轻量背景搜索", prompt)
        self.assertIn("不要把用户整句情绪表达放进搜索词", prompt)
        # Rewrite half kept its rules, including anaphora resolution and boundaries.
        self.assertIn("检索 query rewrite", prompt)
        self.assertIn("消解", prompt)
        self.assertIn("权限边界", prompt)


if __name__ == "__main__":
    unittest.main()
