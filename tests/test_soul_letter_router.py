from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import db, soul_proactive_service
from core.llm import soul_letter_router

DAY = soul_proactive_service.DAY_SECONDS
NOW = 2_000_000_000.0


class FakeCompletions:
    def __init__(self, payloads: list[str]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.payloads.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


class FakeClient:
    def __init__(self, *payloads: str) -> None:
        self.completions = FakeCompletions(list(payloads))
        self.chat = SimpleNamespace(completions=self.completions)


class SoulLetterRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = Path(self.tmp.name) / "workspace"
        db.DB_PATH = db.WORKSPACE_DIR / "state.db"
        db.init_db()
        self._insert_soul("A")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_material_contains_dated_posts_and_user_comments_not_chat(self) -> None:
        self._insert_post("p-1", "科一过了", NOW - 2 * DAY)
        db.execute(
            """
            INSERT INTO comments(
                post_id, soul_name, role, content, seq, created_at
            )
            VALUES ('p-1', 'A', 'user', '补充：考了九十分', 1, ?)
            """,
            (NOW - DAY,),
        )
        db.execute(
            """
            INSERT INTO comments(
                post_id, soul_name, role, content, seq, created_at
            )
            VALUES ('p-1', 'A', 'assistant', '我自己当时的回复', 2, ?)
            """,
            (NOW - 0.5 * DAY,),
        )
        self._insert_soul("B")
        db.execute(
            """
            INSERT INTO comments(
                post_id, soul_name, role, content, seq, created_at
            )
            VALUES ('p-1', 'B', 'assistant', '别的人格的回复', 3, ?)
            """,
            (NOW - 0.4 * DAY,),
        )
        thread_id = self._insert_thread("A")
        db.execute(
            """
            INSERT INTO chat_messages(thread_id, role, content, created_at)
            VALUES (?, 'user', '私聊里的秘密', ?)
            """,
            (thread_id, NOW - DAY),
        )

        material = soul_letter_router.build_letter_material(now=NOW, soul_name="A")

        self.assertEqual(("p-1",), material.post_ids)
        self.assertIn("科一过了", material.text)
        self.assertIn("2 天前", material.text)
        self.assertIn(
            soul_letter_router._absolute_time(NOW - 2 * DAY),
            material.text,
        )
        self.assertIn("补充：考了九十分", material.text)
        self.assertIn("昨天", material.text)
        self.assertIn(
            soul_letter_router._absolute_time(NOW - DAY),
            material.text,
        )
        # 自己回过的那条要在材料里、并标明已经回过——否则模型会对同一条帖子
        # 再写一遍反应，读起来就是它自己那条评论的翻版（实测 8 封里约 3 封）。
        self.assertIn("我自己当时的回复", material.text)
        self.assertIn("【你当时已经回过这条】", material.text)
        # 别的人格说了什么与它无关，进来只会诱导它接话。
        self.assertNotIn("别的人格的回复", material.text)
        self.assertNotIn("私聊里的秘密", material.text)

    def test_material_source_query_excludes_posts_used_by_prior_letters(
        self,
    ) -> None:
        self._insert_post("used", "已经说过的帖子", NOW - 3 * DAY)
        self._insert_post("fresh", "还没说过的帖子", NOW - 2 * DAY)
        db.execute(
            """
            INSERT INTO comments(
                post_id, soul_name, role, content, seq, created_at
            )
            VALUES ('used', 'A', 'user', '已经说过的评论', 1, ?)
            """,
            (NOW - 2.5 * DAY,),
        )
        message_id = self._insert_letter()
        db.execute(
            """
            INSERT INTO soul_message_sources(message_id, post_id)
            VALUES (?, 'used')
            """,
            (message_id,),
        )

        material = soul_letter_router.build_letter_material(now=NOW, soul_name="拾迹者")

        self.assertEqual(("fresh",), material.post_ids)
        self.assertIn("还没说过的帖子", material.text)
        self.assertNotIn("已经说过的帖子", material.text)
        self.assertNotIn("已经说过的评论", material.text)

    def test_f5_blacklist_covers_bare_forms(self) -> None:
        phrases = (
            "想起你科一过了",
            "记起你考过了",
            "有点惦记你",
            "我在等你",
            "一直关注你的进展",
            "这事让人想起你之前念叨的驾校",
            # 生产管线实测逃逸过：副词打头、宾语中间夹一个"来"字，
            # 只锚宾语的那几条全都匹配不上。
            "突然想起来你之前说想学深度学习",
        )

        for message in phrases:
            with self.subTest(message=message):
                parsed = soul_letter_router._parse_letter_response(
                    json.dumps(
                        {"send": True, "message": message},
                        ensure_ascii=False,
                    )
                )
                self.assertEqual(False, parsed["send"])
                self.assertEqual("f5_blacklist", parsed["discarded"])

    def test_f5_blacklist_leaves_user_directed_recall_alone(self) -> None:
        """「想起」「记起」只在自指时才是 F5，对用户说的不算。

        命中即整封丢弃且不重试，所以误伤的代价是白丢一封信——而全局串行冷却下
        三天才可能有一封。裸串匹配会吃掉下面这些完全合法的说法。"""
        # 边界：主语是用户就放行，主语是 SOUL 自己就拦。
        # 「这事让人想起你…」属于后者（声称某事使它想到了用户，仍是虚构的内心
        # 事件），由 test_f5_blacklist_covers_bare_forms 覆盖。
        phrases = (
            "科一过了啊，你要是想起什么想说的随时找我。",
            "科一过了。哪天记起来还有什么没办的，再说。",
        )

        for message in phrases:
            with self.subTest(message=message):
                parsed = soul_letter_router._parse_letter_response(
                    json.dumps(
                        {"send": True, "message": message},
                        ensure_ascii=False,
                    )
                )
                self.assertEqual(True, parsed["send"])
                self.assertEqual(message, parsed["message"])
                self.assertNotIn("discarded", parsed)

    def test_blacklisted_generation_is_discarded_without_retry(self) -> None:
        self._insert_post("p-1", "科一过了", NOW - 8 * DAY)
        client = FakeClient(
            json.dumps(
                {"send": True, "message": "想起你科一过了。"},
                ensure_ascii=False,
            )
        )

        with (
            patch.object(soul_letter_router.db, "now_ts", return_value=NOW),
            patch.object(
                soul_letter_router,
                "now_str",
                return_value="2033 年 05 月 18 日（周三）03:33",
            ),
            patch(
                "core.llm.common.logging_service.log_llm_call"
            ),
        ):
            draft = soul_letter_router.call_soul_letter(
                client,
                "model",
                soul_name="A",
                persona="温和、具体。",
                silent_for_days=8,
            )

        self.assertIsNone(draft)
        self.assertEqual(1, len(client.completions.calls))

    def test_valid_generation_injects_prompt_time_and_returns_material_ids(
        self,
    ) -> None:
        self._insert_post("p-1", "科一过了", NOW - 7 * DAY)
        client = FakeClient(
            json.dumps(
                {"send": True, "message": "科一过了，这一下挺漂亮。"},
                ensure_ascii=False,
            )
        )

        with (
            patch.object(soul_letter_router.db, "now_ts", return_value=NOW),
            patch.object(
                soul_letter_router,
                "now_str",
                return_value="2033 年 05 月 18 日（周三）03:33",
            ),
            patch(
                "core.llm.common.logging_service.log_llm_call"
            ),
        ):
            draft = soul_letter_router.call_soul_letter(
                client,
                "model",
                soul_name="A",
                persona="温和、具体。",
                silent_for_days=7,
            )

        self.assertIsNotNone(draft)
        self.assertEqual("科一过了，这一下挺漂亮。", draft.message)
        self.assertEqual(("p-1",), draft.material_post_ids)
        call = client.completions.calls[0]
        self.assertEqual("model", call["model"])
        self.assertEqual(90, call["timeout"])
        self.assertEqual({"type": "json_object"}, call["response_format"])
        system = call["messages"][0]["content"]
        user = call["messages"][1]["content"]
        self.assertIn("温和、具体。", system)
        self.assertIn("一周 没主动跟 ta 说话", system)
        self.assertIn("2033 年 05 月 18 日（周三）03:33", system)
        self.assertIn("科一过了", user)
        self.assertIn("上周", user)

    def test_send_false_and_empty_material_return_none(self) -> None:
        self._insert_post("p-1", "一条旧动态", NOW - 8 * DAY)
        client = FakeClient('{"send": false, "message": null}')
        with (
            patch.object(soul_letter_router.db, "now_ts", return_value=NOW),
            patch.object(soul_letter_router, "now_str", return_value="当前时间"),
            patch(
                "core.llm.common.logging_service.log_llm_call"
            ),
        ):
            declined = soul_letter_router.call_soul_letter(
                client,
                "model",
                soul_name="A",
                persona="人格",
                silent_for_days=8,
            )

        empty_client = FakeClient()
        db.execute("DELETE FROM posts")
        with patch.object(
            soul_letter_router.db,
            "now_ts",
            return_value=NOW,
        ):
            empty = soul_letter_router.call_soul_letter(
                empty_client,
                "model",
                soul_name="A",
                persona="人格",
                silent_for_days=8,
            )

        self.assertIsNone(declined)
        self.assertIsNone(empty)
        self.assertEqual(1, len(client.completions.calls))
        self.assertEqual(0, len(empty_client.completions.calls))

    @staticmethod
    def _insert_soul(name: str) -> None:
        db.execute(
            """
            INSERT INTO souls(
                name, file_path, enabled, sort_order, created_at, updated_at
            )
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            (name, f"souls/{name}.md", NOW, NOW),
        )

    @staticmethod
    def _insert_post(
        post_id: str,
        content: str,
        created_at: float,
    ) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                post_id,
                soul_letter_router._absolute_time(created_at),
                content,
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def _insert_thread(soul_name: str) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_threads(
                    soul_name, title, created_at, updated_at, last_message_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (soul_name, f"与{soul_name}的私聊", NOW, NOW, NOW),
            )
            return db.require_lastrowid(cursor, "test chat thread")

    def _insert_letter(self) -> int:
        thread_id = self._insert_thread("A")
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_messages(thread_id, role, content, created_at)
                VALUES (?, 'assistant', '上一封信', ?)
                """,
                (thread_id, NOW - 3 * DAY),
            )
            message_id = db.require_lastrowid(cursor, "test soul letter")
            conn.execute(
                """
                INSERT INTO soul_letters(message_id, sent_at)
                VALUES (?, ?)
                """,
                (message_id, NOW - 3 * DAY),
            )
        return message_id


if __name__ == "__main__":
    unittest.main()
