from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from core import query_rewriter
from core.llm import query_rewrite_router


class FakeClient:
    def __init__(self, content: str | None = None, exc: Exception | None = None) -> None:
        self.content = content
        self.exc = exc
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class QueryRewriterTest(unittest.TestCase):
    def test_valid_json_returns_rewrite(self) -> None:
        payload = {
            "semantic_query": "用户是否曾表达过夜晚在图书馆学习效率更高的偏好",
            "keywords": ["晚上", "夜晚", "图书馆", "学习效率", "图书馆"],
        }
        client = FakeClient(json.dumps(payload, ensure_ascii=False))

        result = query_rewriter.rewrite_query(client, "fake-model", "我之前是不是说过晚上图书馆学习效率更高", "public_post")

        self.assertTrue(result.used_rewrite)
        self.assertEqual(payload["semantic_query"], result.semantic_query)
        self.assertEqual(["晚上", "夜晚", "图书馆", "学习效率"], result.keywords)

    def test_invalid_or_empty_result_falls_back(self) -> None:
        client = FakeClient(json.dumps({"semantic_query": "", "keywords": []}, ensure_ascii=False))

        result = query_rewriter.rewrite_query(client, "fake-model", "我之前是不是说过图书馆学习效率更高", "chat")

        self.assertFalse(result.used_rewrite)
        self.assertEqual("我之前是不是说过图书馆学习效率更高", result.semantic_query)
        self.assertEqual([], result.keywords)

    def test_keyword_normalization_caps_and_filters_values(self) -> None:
        payload = {
            "semantic_query": "图书馆学习偏好",
            "keywords": [
                "图书馆",
                "图书馆",
                "a",
                "学习效率特别特别特别特别特别长",
                "晚上",
                "\x00坏值",
                "夜晚",
                "自习室",
                "效率",
                "专注",
                "复习",
                "考试",
                "课程",
                "座位",
                "安静",
            ],
        }
        client = FakeClient(json.dumps(payload, ensure_ascii=False))

        result = query_rewriter.rewrite_query(client, "fake-model", "我之前是不是说过图书馆学习效率更高", "public_post")

        self.assertTrue(result.used_rewrite)
        self.assertEqual(12, len(result.keywords))
        self.assertEqual(1, result.keywords.count("图书馆"))
        self.assertNotIn("a", result.keywords)
        self.assertTrue(all("\x00" not in keyword for keyword in result.keywords))
        self.assertTrue(all(len(keyword) <= 16 for keyword in result.keywords))

    def test_invalid_json_and_api_error_fall_back_without_raising(self) -> None:
        invalid = query_rewriter.rewrite_query(FakeClient("not json"), "fake-model", "我之前是不是说过图书馆学习效率更高", "comment_thread")
        api_error = query_rewriter.rewrite_query(FakeClient(exc=RuntimeError("boom")), "fake-model", "我之前是不是说过图书馆学习效率更高", "comment_thread")

        self.assertFalse(invalid.used_rewrite)
        self.assertFalse(api_error.used_rewrite)
        self.assertEqual("我之前是不是说过图书馆学习效率更高", invalid.semantic_query)
        self.assertEqual("我之前是不是说过图书馆学习效率更高", api_error.semantic_query)

    def test_gate_skips_only_empty_and_slash_commands(self) -> None:
        # Reply paths rewrite every turn; the only skips are empty and slash-commands.
        for raw_query in ["", "   ", "/quit"]:
            with self.subTest(raw_query=raw_query):
                client = FakeClient(json.dumps({"semantic_query": "should not call", "keywords": ["x"]}, ensure_ascii=False))

                result = query_rewriter.rewrite_query(client, "fake-model", raw_query, "public_post")

                self.assertFalse(result.used_rewrite)
                self.assertTrue(result.rewrite_skipped_by_gate)
                self.assertEqual([], client.calls)

    def test_gate_rewrites_short_and_thin_queries(self) -> None:
        # a short/anaphoric message is exactly what needs rewriting — no length gate
        client = FakeClient(json.dumps({"semantic_query": "图书馆偏好", "keywords": ["图书"]}, ensure_ascii=False))

        result = query_rewriter.rewrite_query(client, "fake-model", "图书", "chat")

        self.assertTrue(result.used_rewrite)
        self.assertEqual(1, len(client.calls))

    def test_recent_turns_caps_and_clips(self) -> None:
        messages = [
            SimpleNamespace(role="user", content="第一轮"),
            SimpleNamespace(role="assistant", content=""),  # empty dropped
            SimpleNamespace(role="assistant", content="回应"),
            SimpleNamespace(role="user", content="x" * 500),
        ]
        turns = query_rewriter.recent_turns(messages, limit=3)
        self.assertEqual([{"role": "assistant", "content": "回应"},
                          {"role": "user", "content": "x" * query_rewriter.MAX_TURN_CHARS}], turns)

    def test_rewrite_hands_recent_turns_to_the_model(self) -> None:
        client = FakeClient(json.dumps({"semantic_query": "考研备考进展", "keywords": ["考研"]}, ensure_ascii=False))
        turns = [{"role": "user", "content": "我在准备考研"}, {"role": "assistant", "content": "加油"}]

        result = query_rewriter.rewrite_query(client, "fake-model", "那件事怎么样了", "chat", recent_turns=turns)

        self.assertTrue(result.used_rewrite)
        user_content = client.calls[0]["messages"][1]["content"]
        self.assertIn("我在准备考研", user_content)   # context handed to the model
        self.assertIn("那件事怎么样了", user_content)  # raw query preserved

    def test_long_natural_query_calls_llm(self) -> None:
        payload = {
            "semantic_query": "用户是否曾表达过图书馆学习效率更高",
            "keywords": ["图书馆", "学习效率"],
        }
        client = FakeClient(json.dumps(payload, ensure_ascii=False))

        result = query_rewriter.rewrite_query(client, "fake-model", "我之前是不是说过图书馆学习效率更高", "public_post")

        self.assertTrue(result.used_rewrite)
        self.assertEqual(1, len(client.calls))

    def test_prompt_forbids_answering_or_changing_boundaries(self) -> None:
        prompt = query_rewrite_router.QUERY_REWRITE_PROMPT

        self.assertIn("不要回答用户问题", prompt)
        self.assertIn("不要改变", prompt)
        self.assertIn("权限边界", prompt)


if __name__ == "__main__":
    unittest.main()
