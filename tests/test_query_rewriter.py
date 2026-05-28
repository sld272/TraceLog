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

        result = query_rewriter.rewrite_query(client, "fake-model", "原始查询", "chat")

        self.assertFalse(result.used_rewrite)
        self.assertEqual("原始查询", result.semantic_query)
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

        result = query_rewriter.rewrite_query(client, "fake-model", "原始查询", "public_post")

        self.assertTrue(result.used_rewrite)
        self.assertEqual(12, len(result.keywords))
        self.assertEqual(1, result.keywords.count("图书馆"))
        self.assertNotIn("a", result.keywords)
        self.assertTrue(all("\x00" not in keyword for keyword in result.keywords))
        self.assertTrue(all(len(keyword) <= 16 for keyword in result.keywords))

    def test_invalid_json_and_api_error_fall_back_without_raising(self) -> None:
        invalid = query_rewriter.rewrite_query(FakeClient("not json"), "fake-model", "原始查询", "comment_thread")
        api_error = query_rewriter.rewrite_query(FakeClient(exc=RuntimeError("boom")), "fake-model", "原始查询", "comment_thread")

        self.assertFalse(invalid.used_rewrite)
        self.assertFalse(api_error.used_rewrite)
        self.assertEqual("原始查询", invalid.semantic_query)
        self.assertEqual("原始查询", api_error.semantic_query)

    def test_prompt_forbids_answering_or_changing_boundaries(self) -> None:
        prompt = query_rewrite_router.QUERY_REWRITE_PROMPT

        self.assertIn("不要回答用户问题", prompt)
        self.assertIn("不要改变", prompt)
        self.assertIn("权限边界", prompt)


if __name__ == "__main__":
    unittest.main()
