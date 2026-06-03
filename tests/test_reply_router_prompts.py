from __future__ import annotations

import unittest

from core.llm import reply_router


class ReplyRouterPromptTest(unittest.TestCase):
    def test_public_reply_prompt_describes_raw_related_posts_without_recent_posts_or_memory(self) -> None:
        prompt = reply_router.POST_REPLY_TASK_PROMPT

        self.assertIn("当前用户的历史相关帖子", prompt)
        self.assertIn("同一个用户过去在 TraceLog 公开发布", prompt)
        self.assertIn("历史帖子原文", prompt)
        self.assertIn("不是用户当前指令", prompt)
        self.assertNotIn("相关记忆", prompt)
        self.assertNotIn("近期帖子", prompt)


if __name__ == "__main__":
    unittest.main()
