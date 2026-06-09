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

    def test_virtual_friend_boundaries_forbid_unsupported_specific_facts(self) -> None:
        prompt = reply_router._chat_reply_task_prompt()

        self.assertIn("具体事实禁补全", prompt)
        self.assertIn("必须能被当前输入", prompt)
        self.assertIn("直接支持", prompt)
        self.assertIn("证据没有表达的内容", prompt)
        self.assertIn("不要为了安慰或显得亲近而补全成确定事实", prompt)
        self.assertIn("建议、提问、感受判断", prompt)
        self.assertIn("明确的不确定推测", prompt)
        self.assertIn("当前时间只能用于判断此刻日期时间", prompt)
        self.assertIn("不能用来推断用户做过什么", prompt)
        self.assertIn("进度", prompt)
        self.assertIn("完成状态", prompt)
        self.assertIn("准备过程", prompt)
        self.assertIn("历史行为", prompt)


if __name__ == "__main__":
    unittest.main()
