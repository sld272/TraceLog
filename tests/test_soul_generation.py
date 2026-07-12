from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import web_search_service
from core.llm import soul_router


SOUL_MARKDOWN = (
    "---\nname: 测试好友\nversion: 1\ndescription: 测试\n---\n\n"
    "你是 TraceLog 中名为「测试好友」的 AI 好友。\n\n"
    "## 语气特征\n测试\n\n## 怎么回应\n测试\n\n## 边界\n测试"
)

GATE_YES = json.dumps(
    {
        "should_search": True,
        "queries": ["芙莉莲 葬送的芙莉莲 角色 性格"],
        "reason": "灵感引用具名公开角色",
        "freshness_required": False,
    },
    ensure_ascii=False,
)

GATE_NO = json.dumps(
    {"should_search": False, "queries": [], "reason": "抽象性格描述", "freshness_required": False},
    ensure_ascii=False,
)

SOUL_JSON = json.dumps({"soul": SOUL_MARKDOWN}, ensure_ascii=False)


class FakeClient:
    """Returns queued response contents in order, recording every call."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responses.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _search_config(enabled: bool = True) -> web_search_service.WebSearchConfig:
    return web_search_service.WebSearchConfig(
        enabled=enabled,
        provider="tavily",
        tavily_api_key="tvly-test" if enabled else None,
        max_results=5,
        timeout_s=8,
        cache_ttl_s=1800,
    )


def _search_run(used: bool = True) -> web_search_service.WebSearchRun:
    results = (
        [
            web_search_service.WebSearchResult(
                title="芙莉莲 - 角色介绍",
                url="https://example.com/frieren",
                snippet="千年精灵魔法使，语气平淡……",
                provider="tavily",
            )
        ]
        if used
        else []
    )
    return web_search_service.WebSearchRun(
        used=used,
        provider="tavily",
        queries=["芙莉莲 葬送的芙莉莲 角色 性格"],
        results=results,
        error=None,
        elapsed_ms=5,
    )


class SoulGenerationSearchTest(unittest.TestCase):
    def test_generate_injects_reference_and_reports_sources(self) -> None:
        client = FakeClient([GATE_YES, SOUL_JSON])
        with (
            patch.object(soul_router.web_search_service, "effective_config", return_value=_search_config()),
            patch.object(soul_router.web_search_service, "select_provider", return_value="tavily"),
            patch.object(soul_router.web_search_service, "search", return_value=_search_run()) as search_mock,
        ):
            result = soul_router.generate_soul(
                name="测试好友",
                inspiration="像《葬送的芙莉莲》里的芙莉莲",
                client=client,
                model="test-model",
            )

        self.assertIsNotNone(result)
        self.assertEqual(SOUL_MARKDOWN, result["soul"])
        self.assertTrue(result["search_used"])
        self.assertEqual(
            [{"title": "芙莉莲 - 角色介绍", "url": "https://example.com/frieren"}],
            result["sources"],
        )
        search_mock.assert_called_once()
        # 第二次 LLM 调用（生成）应包含搜索资料段
        self.assertEqual(2, len(client.calls))
        generation_prompt = client.calls[1]["messages"][1]["content"]
        self.assertIn("网页搜索结果", generation_prompt)
        self.assertIn("芙莉莲 - 角色介绍", generation_prompt)

    def test_generate_skips_gate_when_search_unavailable(self) -> None:
        client = FakeClient([SOUL_JSON])
        with (
            patch.object(soul_router.web_search_service, "effective_config", return_value=_search_config(False)),
            patch.object(soul_router.web_search_service, "select_provider", return_value=None),
            patch.object(soul_router.web_search_service, "search") as search_mock,
        ):
            result = soul_router.generate_soul(
                name="测试好友",
                inspiration="温柔但不纵容",
                client=client,
                model="test-model",
            )

        self.assertIsNotNone(result)
        self.assertFalse(result["search_used"])
        self.assertEqual([], result["sources"])
        search_mock.assert_not_called()
        self.assertEqual(1, len(client.calls))

    def test_generate_degrades_when_gate_declines(self) -> None:
        client = FakeClient([GATE_NO, SOUL_JSON])
        with (
            patch.object(soul_router.web_search_service, "effective_config", return_value=_search_config()),
            patch.object(soul_router.web_search_service, "select_provider", return_value="tavily"),
            patch.object(soul_router.web_search_service, "search") as search_mock,
        ):
            result = soul_router.generate_soul(
                name="测试好友",
                inspiration="温柔但不纵容",
                client=client,
                model="test-model",
            )

        self.assertIsNotNone(result)
        self.assertFalse(result["search_used"])
        search_mock.assert_not_called()

    def test_generate_degrades_when_gate_returns_invalid_json(self) -> None:
        client = FakeClient(["not json at all", SOUL_JSON])
        with (
            patch.object(soul_router.web_search_service, "effective_config", return_value=_search_config()),
            patch.object(soul_router.web_search_service, "select_provider", return_value="tavily"),
            patch.object(soul_router.web_search_service, "search") as search_mock,
        ):
            result = soul_router.generate_soul(
                name="测试好友",
                inspiration="像《葬送的芙莉莲》里的芙莉莲",
                client=client,
                model="test-model",
            )

        self.assertIsNotNone(result)
        self.assertFalse(result["search_used"])
        search_mock.assert_not_called()

    def test_generate_degrades_when_search_raises(self) -> None:
        client = FakeClient([GATE_YES, SOUL_JSON])
        with (
            patch.object(soul_router.web_search_service, "effective_config", return_value=_search_config()),
            patch.object(soul_router.web_search_service, "select_provider", return_value="tavily"),
            patch.object(soul_router.web_search_service, "search", side_effect=RuntimeError("boom")),
        ):
            result = soul_router.generate_soul(
                name="测试好友",
                inspiration="像《葬送的芙莉莲》里的芙莉莲",
                client=client,
                model="test-model",
            )

        self.assertIsNotNone(result)
        self.assertEqual(SOUL_MARKDOWN, result["soul"])
        self.assertFalse(result["search_used"])

    def test_generate_handles_zero_search_results(self) -> None:
        client = FakeClient([GATE_YES, SOUL_JSON])
        with (
            patch.object(soul_router.web_search_service, "effective_config", return_value=_search_config()),
            patch.object(soul_router.web_search_service, "select_provider", return_value="tavily"),
            patch.object(soul_router.web_search_service, "search", return_value=_search_run(False)),
        ):
            result = soul_router.generate_soul(
                name="测试好友",
                inspiration="像《葬送的芙莉莲》里的芙莉莲",
                client=client,
                model="test-model",
            )

        self.assertIsNotNone(result)
        self.assertFalse(result["search_used"])
        generation_prompt = client.calls[1]["messages"][1]["content"]
        self.assertNotIn("网页搜索结果", generation_prompt)


class SoulRevisionTest(unittest.TestCase):
    def test_revise_soul_never_searches(self) -> None:
        client = FakeClient([SOUL_JSON])
        with (
            patch.object(soul_router.web_search_service, "effective_config") as config_mock,
            patch.object(soul_router.web_search_service, "search") as search_mock,
        ):
            result = soul_router.revise_soul(
                name="测试好友",
                current_soul=SOUL_MARKDOWN,
                feedback="语气再毒舌一点",
                client=client,
                model="test-model",
            )

        self.assertIsNotNone(result)
        self.assertEqual(SOUL_MARKDOWN, result["soul"])
        self.assertFalse(result["search_used"])
        self.assertEqual([], result["sources"])
        config_mock.assert_not_called()
        search_mock.assert_not_called()
        self.assertEqual(1, len(client.calls))
        prompt = client.calls[0]["messages"][1]["content"]
        self.assertIn("语气再毒舌一点", prompt)
        self.assertIn("BEGIN CURRENT SOUL", prompt)

    def test_revise_soul_returns_none_on_invalid_response(self) -> None:
        client = FakeClient(["not json"])
        result = soul_router.revise_soul(
            name="测试好友",
            current_soul=SOUL_MARKDOWN,
            feedback="语气再毒舌一点",
            client=client,
            model="test-model",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
