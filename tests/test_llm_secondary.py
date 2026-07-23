from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from core import web_search_gate
from core.llm import query_rewrite_router, secondary_model, suggestion_router


class FakeClient:
    def __init__(self, payload: dict | str) -> None:
        self.payload = payload
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class SecondaryModelConfigTest(unittest.TestCase):
    def tearDown(self) -> None:
        secondary_model.reset()

    def test_effective_config_is_none_without_secondary_model(self) -> None:
        self.assertIsNone(secondary_model.effective_config(None))
        self.assertIsNone(secondary_model.effective_config({}))
        self.assertIsNone(secondary_model.effective_config({"secondary_model": "   "}))

    def test_effective_config_falls_back_to_main_credentials(self) -> None:
        settings = secondary_model.effective_config(
            {
                "api_key": "sk-main",
                "base_url": "https://main.invalid/v1",
                "secondary_model": " fast-mini ",
            }
        )

        self.assertEqual(
            {"model": "fast-mini", "api_key": "sk-main", "base_url": "https://main.invalid/v1"},
            settings,
        )

    def test_effective_config_prefers_own_credentials(self) -> None:
        settings = secondary_model.effective_config(
            {
                "api_key": "sk-main",
                "base_url": "https://main.invalid/v1",
                "secondary_model": "fast-mini",
                "secondary_api_key": "sk-secondary",
                "secondary_base_url": "https://fast.invalid/v1",
            }
        )

        self.assertEqual(
            {"model": "fast-mini", "api_key": "sk-secondary", "base_url": "https://fast.invalid/v1"},
            settings,
        )

    def test_resolve_returns_main_pair_when_not_installed(self) -> None:
        main = FakeClient({})

        client, model = secondary_model.resolve(main, "main-model")

        self.assertIs(main, client)
        self.assertEqual("main-model", model)
        self.assertFalse(secondary_model.is_configured())

    def test_install_reuses_main_client_for_same_credentials(self) -> None:
        main = FakeClient({})
        factory_calls: list[tuple[str, str]] = []

        def factory(api_key: str, base_url: str) -> FakeClient:
            factory_calls.append((api_key, base_url))
            return FakeClient({})

        secondary_model.install_from_config(
            {
                "api_key": "sk-main",
                "base_url": "https://main.invalid/v1",
                "secondary_model": "fast-mini",
            },
            main_client=main,
            client_factory=factory,
        )

        client, model = secondary_model.resolve(main, "main-model")
        self.assertIs(main, client)
        self.assertEqual("fast-mini", model)
        self.assertEqual([], factory_calls)

    def test_install_builds_separate_client_for_own_credentials(self) -> None:
        main = FakeClient({})
        separate = FakeClient({})
        factory_calls: list[tuple[str, str]] = []

        def factory(api_key: str, base_url: str) -> FakeClient:
            factory_calls.append((api_key, base_url))
            return separate

        secondary_model.install_from_config(
            {
                "api_key": "sk-main",
                "base_url": "https://main.invalid/v1",
                "secondary_model": "fast-mini",
                "secondary_api_key": "sk-secondary",
                "secondary_base_url": "https://fast.invalid/v1",
            },
            main_client=main,
            client_factory=factory,
        )

        client, model = secondary_model.resolve(main, "main-model")
        self.assertIs(separate, client)
        self.assertEqual("fast-mini", model)
        self.assertEqual([("sk-secondary", "https://fast.invalid/v1")], factory_calls)

    def test_install_resets_when_secondary_model_removed(self) -> None:
        main = FakeClient({})
        secondary_model.configure(FakeClient({}), "fast-mini")

        secondary_model.install_from_config(
            {"api_key": "sk-main", "base_url": "https://main.invalid/v1"},
            main_client=main,
            client_factory=lambda api_key, base_url: FakeClient({}),
        )

        client, model = secondary_model.resolve(main, "main-model")
        self.assertIs(main, client)
        self.assertEqual("main-model", model)


class SecondaryModelRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.main = FakeClient({})
        self.secondary = FakeClient(
            {"should_search": False, "queries": [], "reason": "闲聊", "freshness_required": False}
        )
        secondary_model.configure(self.secondary, "fast-mini")

    def tearDown(self) -> None:
        secondary_model.reset()

    def _assert_routed_to_secondary(self) -> None:
        self.assertEqual([], self.main.calls)
        self.assertEqual(1, len(self.secondary.calls))
        self.assertEqual("fast-mini", self.secondary.calls[0]["model"])

    def test_web_search_gate_uses_secondary_model(self) -> None:
        web_search_gate.decide(self.main, "main-model", "查一下今天的新闻", channel="chat")

        self._assert_routed_to_secondary()

    def test_query_rewrite_uses_secondary_model(self) -> None:
        query_rewrite_router.call_query_rewrite(
            self.main, "main-model", raw_query="那件事后来怎么样了", channel="chat"
        )

        self._assert_routed_to_secondary()

    def test_suggestion_router_uses_secondary_model(self) -> None:
        suggestion_router.call_suggestion_router(
            self.main, "main-model", user_input="我决定这学期把绩点提到 3.7"
        )

        self._assert_routed_to_secondary()


class TimeAnnotationInjectionTest(unittest.TestCase):
    """目标抽取在 user 消息末尾注入「系统按说话时刻计算」的时间标注区块。"""

    def setUp(self) -> None:
        secondary_model.reset()

    def tearDown(self) -> None:
        secondary_model.reset()

    def _user_message(self, client: FakeClient) -> str:
        self.assertEqual(1, len(client.calls))
        return client.calls[0]["messages"][1]["content"]

    def test_suggestion_router_injects_time_annotation_block(self) -> None:
        client = FakeClient({"goals": [], "events": []})
        suggestion_router.call_suggestion_router(
            client, "m", user_input="打算周五前交报告，这学期还要把绩点提到 3.7"
        )
        user_msg = self._user_message(client)
        self.assertIn("## 时间标注（系统按说话时刻计算）", user_msg)
        self.assertIn("周五＝", user_msg)

    def test_no_block_when_text_has_no_relative_time(self) -> None:
        client = FakeClient({"goals": [], "events": []})
        suggestion_router.call_suggestion_router(
            client, "m", user_input="我决定认真准备研究生考试"
        )
        self.assertNotIn("时间标注", self._user_message(client))


if __name__ == "__main__":
    unittest.main()
