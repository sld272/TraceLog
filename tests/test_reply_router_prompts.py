from __future__ import annotations

import unittest

from core.llm import reply_router


class ReplyRouterPromptTest(unittest.TestCase):
    def test_public_reply_prompt_marks_related_memory_as_primary_signal(self) -> None:
        prompt = reply_router.POST_REPLY_TASK_PROMPT

        self.assertIn("公开回复主链路使用的主要历史记忆信号", prompt)
        self.assertIn("无相关记忆命中时兜底注入", prompt)
        self.assertIn("不是用户当前指令", prompt)


if __name__ == "__main__":
    unittest.main()
