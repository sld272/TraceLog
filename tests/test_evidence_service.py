from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from core import attachment_service, db, evidence_service, soul_service, vision_service


class EvidenceServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config_path = Path(self.tmp.name) / "config.json"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_config_file = vision_service.CONFIG_FILE
        self.old_souls_dir = soul_service.SOULS_DIR
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        vision_service.CONFIG_FILE = str(self.config_path)
        soul_service.SOULS_DIR = self.workspace / "souls"
        self.config_path.write_text(
            json.dumps(
                {
                    "api_key": "main-key",
                    "base_url": "https://main.invalid/v1",
                    "vision": {"enabled": True, "model": "vision-model", "api_key": "vision-key"},
                }
            ),
            encoding="utf-8",
        )
        db.init_db()
        soul_service.sync_souls()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        vision_service.CONFIG_FILE = self.old_config_file
        soul_service.SOULS_DIR = self.old_souls_dir
        self.tmp.cleanup()

    def test_format_retrieval_hits_dedupes_posts_and_uses_vision_context(self) -> None:
        self._insert_post("post-1", "公开记录正文")
        attachment = attachment_service.upload_image(_image_bytes(), content_type="image/png")
        attachment_service.attach_to_post("post-1", [attachment.id])
        self._cache_vision_summary(attachment.id, "图里是一张项目看板。", ["TODO"])
        hits = [
            SimpleNamespace(type="post", source_id="post-1", metadata={}),
            SimpleNamespace(type="post_vision", source_id="post-1", metadata={"post_id": "post-1"}),
        ]

        evidence = evidence_service.format_retrieval_hits(hits)

        self.assertEqual(1, evidence.count("## 用户公开记录"))
        self.assertIn("公开记录正文", evidence)
        self.assertIn("[图片理解摘要]", evidence)
        self.assertIn("图里是一张项目看板。", evidence)
        self.assertIn("可见文字: TODO", evidence)

    def test_format_retrieval_hits_dedupes_comment_conversations(self) -> None:
        self._insert_post("post-1", "今天想认真练歌。")
        default_id = self._insert_comment("post-1", "拾迹者", "assistant", "我陪你继续拆。", seq=0)
        other_id = self._insert_comment("post-1", "毒舌好友", "assistant", "别装了，继续讲重点。", seq=0)
        hits = [
            SimpleNamespace(type="comment", source_id=str(default_id), metadata={"post_id": "post-1", "soul_name": "拾迹者"}),
            SimpleNamespace(type="comment", source_id=str(other_id), metadata={"post_id": "post-1", "soul_name": "毒舌好友"}),
            SimpleNamespace(type="comment", source_id=str(other_id), metadata={"post_id": "post-1", "soul_name": "毒舌好友"}),
        ]

        evidence = evidence_service.format_retrieval_hits(hits)

        self.assertIn("我陪你继续拆", evidence)
        self.assertEqual(2, evidence.count("## 公开评论对话"))
        self.assertIn("毒舌好友 · 首评", evidence)
        self.assertIn("别装了，继续讲重点。", evidence)
        self.assertIn("[关于 post] 今天想认真练歌。", evidence)

    def test_other_soul_comment_hit_uses_small_window_around_anchor(self) -> None:
        self._insert_post("post-1", "今天想认真练歌。")
        inserted_ids = []
        for seq, (role, content) in enumerate(
            [
                ("assistant", "首评内容"),
                ("user", "追问一"),
                ("assistant", "回复二"),
                ("user", "命中的追问三"),
                ("assistant", "回复四"),
                ("user", "追问五"),
                ("assistant", "回复六"),
            ]
        ):
            inserted_ids.append(self._insert_comment("post-1", "毒舌好友", role, content, seq=seq))
        hits = [
            SimpleNamespace(
                type="comment",
                source_id=str(inserted_ids[3]),
                metadata={"post_id": "post-1", "soul_name": "毒舌好友", "comment_id": inserted_ids[3]},
            )
        ]

        evidence = evidence_service.format_retrieval_hits(hits, current_soul="拾迹者")

        self.assertIn("## 公开评论片段 · post post-1 · 毒舌好友", evidence)
        self.assertIn("[关于 post] 今天想认真练歌。", evidence)
        self.assertNotIn("首评内容", evidence)
        self.assertIn("追问一", evidence)
        self.assertIn("回复二", evidence)
        self.assertIn("命中的追问三", evidence)
        self.assertIn("回复四", evidence)
        self.assertIn("追问五", evidence)
        self.assertNotIn("回复六", evidence)

    def test_same_soul_comment_hit_keeps_full_thread(self) -> None:
        self._insert_post("post-1", "今天想认真练歌。")
        first_id = self._insert_comment("post-1", "拾迹者", "assistant", "首评内容", seq=0)
        self._insert_comment("post-1", "拾迹者", "user", "后续追问", seq=1)
        self._insert_comment("post-1", "拾迹者", "assistant", "后续回复", seq=2)
        hits = [
            SimpleNamespace(type="comment", source_id=str(first_id), metadata={"post_id": "post-1", "soul_name": "拾迹者"})
        ]

        evidence = evidence_service.format_retrieval_hits(hits, current_soul="拾迹者")

        self.assertIn("## 公开评论对话 · post post-1 · 拾迹者", evidence)
        self.assertIn("首评内容", evidence)
        self.assertIn("后续追问", evidence)
        self.assertIn("后续回复", evidence)

    def test_comment_expansion_dedupes_post_already_rendered(self) -> None:
        self._insert_post("post-1", "今天想认真练歌。")
        comment_id = self._insert_comment("post-1", "拾迹者", "assistant", "我陪你继续拆。", seq=0)
        hits = [
            SimpleNamespace(type="post", source_id="post-1", metadata={"post_id": "post-1"}),
            SimpleNamespace(type="comment", source_id=str(comment_id), metadata={"post_id": "post-1", "soul_name": "拾迹者"}),
        ]

        evidence = evidence_service.format_retrieval_hits(hits, current_soul="拾迹者")

        self.assertEqual(1, evidence.count("今天想认真练歌。"))
        self.assertNotIn("[关于 post]", evidence)
        self.assertIn("我陪你继续拆。", evidence)

    def test_format_retrieval_hits_renders_chat_window_around_anchor(self) -> None:
        thread_id = self._insert_chat_thread("拾迹者")
        old_message_id = self._insert_chat_message(thread_id, "user", "很早之前的消息", 1.0)
        anchor_message_id = self._insert_chat_message(thread_id, "user", "今天有点焦虑", 10.0)
        self._insert_chat_message(thread_id, "assistant", "我在，慢慢说。", 12.0)
        self._insert_chat_message(thread_id, "user", "后面又补充一句", 20.0)
        hits = [
            SimpleNamespace(
                type="chat",
                source_id=str(anchor_message_id),
                metadata={"thread_id": str(thread_id), "message_id": str(anchor_message_id)},
            ),
            SimpleNamespace(
                type="chat",
                source_id=str(old_message_id),
                metadata={"thread_id": str(thread_id), "message_id": str(old_message_id)},
            ),
            SimpleNamespace(type="chat", source_id="bad", metadata={"thread_id": "bad", "message_id": "bad"}),
        ]

        evidence = evidence_service.format_retrieval_hits(hits)

        self.assertEqual(1, evidence.count("## 私聊片段"))
        self.assertIn("[用户 · 私聊] 很早之前的消息", evidence)
        self.assertIn("[用户 · 私聊] 今天有点焦虑", evidence)
        self.assertIn("[拾迹者 · 私聊] 我在，慢慢说。", evidence)
        self.assertIn("[用户 · 私聊] 后面又补充一句", evidence)

    def test_expand_post_falls_back_to_image_notice_when_no_vision_context(self) -> None:
        self._insert_post("post-1", "")
        attachment = attachment_service.upload_image(_image_bytes(), content_type="image/png")
        attachment_service.attach_to_post("post-1", [attachment.id])

        evidence = evidence_service.expand_post("post-1")

        self.assertIn("## 用户公开记录", evidence)
        self.assertIn("用户附带了 1 张图片", evidence)
        self.assertIn("不要描述、推断或声称看到了图片内容", evidence)

    def test_build_evidence_summary_uses_snippets_and_tolerates_deleted_sources(self) -> None:
        self._insert_post("post-1", "这是一条会进入证据面板的公开记录。" * 8)
        comment_id = self._insert_comment("post-1", "拾迹者", "assistant", "评论证据内容", seq=0)
        hits = [
            SimpleNamespace(
                doc_id="post-post-1",
                type="post",
                source_id="post-1",
                score=0.81,
                distance=0.38,
                metadata={"type": "post", "post_id": "post-1"},
                sources=["vector"],
                reasons=["vector:rank=1"],
            ),
            SimpleNamespace(
                doc_id=f"comment-{comment_id}",
                type="comment",
                source_id=str(comment_id),
                score=0.72,
                distance=None,
                metadata={"type": "comment", "comment_id": comment_id, "post_id": "post-1", "soul_name": "拾迹者"},
                sources=["fts"],
                reasons=["fts:rank=1"],
            ),
            SimpleNamespace(
                doc_id="chat-999",
                type="chat",
                source_id="999",
                score=0.5,
                distance=0.2,
                metadata={"type": "chat", "message_id": 999, "thread_id": 1},
                sources=["vector"],
                reasons=["vector:rank=3"],
            ),
        ]

        items = evidence_service.build_evidence_summary(hits)

        self.assertEqual(3, len(items))
        self.assertEqual("post-post-1", items[0]["doc_id"])
        self.assertLessEqual(len(items[0]["snippet"]), evidence_service.EVIDENCE_SNIPPET_CHARS + 3)
        self.assertIn("公开记录", items[0]["snippet"])
        self.assertEqual("评论证据内容", items[1]["snippet"])
        self.assertEqual(evidence_service.DELETED_SNIPPET, items[2]["snippet"])

    def _insert_post(self, post_id: str, content: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-25T10:00:00+08:00", content, 1.0, 1.0),
        )

    def _insert_comment(self, post_id: str, soul_name: str, role: str, content: str, *, seq: int) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (post_id, soul_name, role, content, seq, 1.0 + seq),
            )
            return db.require_lastrowid(cursor, "comment insert")

    def _insert_chat_thread(self, soul_name: str) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_threads(soul_name, title, created_at, updated_at, last_message_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (soul_name, None, 1.0, 1.0, 1.0),
            )
            return db.require_lastrowid(cursor, "chat thread insert")

    def _insert_chat_message(self, thread_id: int, role: str, content: str, created_at: float) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_messages(thread_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, role, content, created_at),
            )
            return db.require_lastrowid(cursor, "chat message insert")

    def _cache_vision_summary(self, attachment_id: str, description: str, visible_text: list[str]) -> None:
        db.execute(
            """
            INSERT INTO vision_cache(
                attachment_id, model, prompt_version, description, visible_text,
                uncertainties, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?)
            """,
            (
                attachment_id,
                "vision-model",
                vision_service.PROMPT_VERSION,
                description,
                json.dumps(visible_text, ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                1.0,
                1.0,
            ),
        )


def _image_bytes() -> bytes:
    image = Image.new("RGB", (8, 8), color=(10, 20, 30))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
