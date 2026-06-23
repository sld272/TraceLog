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
        self.assertIn("当前帖子优先规则", prompt)
        self.assertIn("第一句话应直接贴合当前帖子", prompt)
        self.assertIn("不要把它们当作本次要回复的帖子主体", prompt)
        self.assertIn("不要让历史话题抢占回复重心", prompt)
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

    def test_comment_reply_prompt_forbids_exposing_private_chat_in_public_comments(self) -> None:
        prompt = reply_router.COMMENT_REPLY_TASK_PROMPT

        self.assertIn("私聊边界", prompt)
        self.assertIn("标注为「私聊片段」", prompt)
        self.assertIn("不要点破、复述或直接引用私聊内容", prompt)
        self.assertIn("表达上必须像是只基于公开信息", prompt)

    def test_comment_reply_prompt_allows_public_other_soul_context_with_boundaries(self) -> None:
        prompt = reply_router.COMMENT_REPLY_TASK_PROMPT

        # reply centers on the user's latest message in your OWN thread; other
        # SOULs' threads are background you may reference only when DIRECTLY
        # relevant (e.g. a contradiction), not gratuitously
        self.assertIn("回复主体是你自己那条", prompt)
        self.assertIn("对你说的最后一句话", prompt)
        self.assertIn("用户对 X 说", prompt)
        self.assertIn("默认不要把那边的话题扯进来", prompt)
        self.assertIn("直接相关", prompt)
        self.assertIn("自相矛盾", prompt)
        self.assertIn("我看到你跟 X 说", prompt)
        self.assertIn("不要直接与其他 SOUL 对话", prompt)
        self.assertIn("不要替其他 SOUL 发言", prompt)

    def test_comment_reply_subject_anchor_pins_current_message(self) -> None:
        anchor = reply_router._comment_reply_subject_anchor("实则躺平")
        self.assertIn("「实则躺平」", anchor)
        self.assertIn("回复主体", anchor)            # primary subject = current message
        self.assertIn("默认不要扯到那些话题", anchor)  # default: stay on this thread
        self.assertIn("直接相关", anchor)             # but may reference when relevant
        self.assertIn("自相矛盾", anchor)             # the contradiction case
        self.assertIn("天天内卷", anchor)             # calibration example
        # nothing to anchor when there is no current user message
        self.assertEqual(reply_router._comment_reply_subject_anchor("  "), "")


if __name__ == "__main__":
    unittest.main()
