from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (
    chat_service,
    comment_service,
    db,
    memory_read,
    record_service,
)
from core.app_services import job_service, post_mutation


def _events(source_type: str, source_id: str) -> list:
    return db.query_all(
        """
        SELECT * FROM memory_ingest_events
        WHERE source_type = ? AND source_id = ?
        ORDER BY source_revision ASC
        """,
        (source_type, str(source_id)),
    )


class MemoryEventsWiringTest(unittest.TestCase):
    """Phase 1 part 2: every live mutation appends an evidence event in-txn."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        souls_dir = self.workspace / "souls"
        souls_dir.mkdir(parents=True, exist_ok=True)
        (souls_dir / "luna.md").write_text("# Luna\n陪伴型人格。", encoding="utf-8")
        now = db.now_ts()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO souls(name, file_path, enabled, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                ("luna", "souls/luna.md", now, now),
            )

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    # --- posts -------------------------------------------------------------

    def test_save_post_appends_create_event(self) -> None:
        post_id = record_service.save_post("考研倒计时", index_immediately=False, track_embedding=False)
        events = _events("post", post_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["op"], "create")
        self.assertEqual(events[0]["owner_scope"], "global")
        self.assertEqual(events[0]["visibility_scope"], "public")
        self.assertEqual(events[0]["content_snapshot"], "考研倒计时")

    def test_delete_post_appends_delete_event(self) -> None:
        post_id = record_service.save_post("临时帖", index_immediately=False, track_embedding=False)
        post_mutation.delete_post(post_id)
        events = _events("post", post_id)
        self.assertEqual([e["op"] for e in events], ["create", "delete"])
        self.assertEqual(events[1]["source_revision"], 2)
        self.assertIsNone(events[1]["content_snapshot"])

    def test_delete_post_appends_delete_events_for_cascaded_comments(self) -> None:
        post_id = record_service.save_post("帖子", index_immediately=False, track_embedding=False)
        self._seed_root_comment(post_id)
        user = comment_service.append_comment(post_id, "luna", "user", "会一起删除")

        post_mutation.delete_post(post_id)

        root = db.query_one(
            "SELECT source_id FROM memory_ingest_events "
            "WHERE source_type = 'comment_message' AND author = 'assistant' LIMIT 1"
        )
        self.assertEqual(
            [row["op"] for row in _events("comment_message", root["source_id"])],
            ["delete"],
        )
        self.assertEqual(
            [row["op"] for row in _events("comment_message", str(user.id))],
            ["create", "delete"],
        )

    # --- comments ----------------------------------------------------------

    def _seed_root_comment(self, post_id: str) -> None:
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
                VALUES (?, 'luna', 'assistant', '首评', 0, ?)
                """,
                (post_id, db.now_ts()),
            )

    def test_append_user_comment_appends_create_event(self) -> None:
        post_id = record_service.save_post("帖子", index_immediately=False, track_embedding=False)
        self._seed_root_comment(post_id)
        msg = comment_service.append_comment(post_id, "luna", "user", "追问一下")
        events = _events("comment_message", str(msg.id))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["op"], "create")
        self.assertEqual(events[0]["owner_scope"], "soul:luna")  # owned by the thread's soul
        self.assertEqual(events[0]["author"], "user")
        self.assertEqual(events[0]["visibility_scope"], f"thread:{post_id}")

    def test_delete_comment_appends_delete_events(self) -> None:
        post_id = record_service.save_post("帖子", index_immediately=False, track_embedding=False)
        self._seed_root_comment(post_id)
        msg = comment_service.append_comment(post_id, "luna", "user", "要被删的追问")
        comment_service.delete_message(msg.id)
        events = _events("comment_message", str(msg.id))
        self.assertEqual([e["op"] for e in events], ["create", "delete"])

    def test_delete_comment_enqueues_reconcile_in_v2_write_mode(self) -> None:
        post_id = record_service.save_post("帖子", index_immediately=False, track_embedding=False)
        self._seed_root_comment(post_id)
        msg = comment_service.append_comment(post_id, "luna", "user", "要删除")
        with patch.dict(os.environ, {memory_read.WRITE_MODE_ENV: "reconcile"}):
            comment_service.delete_message(msg.id)
        self.assertEqual(
            [row["type"] for row in db.query_all("SELECT type FROM jobs")],
            [job_service.TYPE_RUN_MEMORY_RECONCILE],
        )

    # --- chat --------------------------------------------------------------

    def _seed_thread(self) -> int:
        now = db.now_ts()
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO chat_threads(soul_name, created_at, updated_at) VALUES ('luna', ?, ?)",
                (now, now),
            )
            return db.require_lastrowid(cursor, "thread insert")

    def test_chat_append_create_event_is_private(self) -> None:
        thread_id = self._seed_thread()
        msg = chat_service._append_message(thread_id, "user", "晚安")
        events = _events("chat_message", str(msg.id))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["op"], "create")
        self.assertEqual(events[0]["owner_scope"], "soul:luna")
        self.assertEqual(events[0]["visibility_scope"], "private:soul:luna")

    def test_chat_edit_user_message_edits_and_cascades_delete(self) -> None:
        thread_id = self._seed_thread()
        user_msg = chat_service._append_message(thread_id, "user", "原始问题")
        assistant_msg = chat_service._append_message(thread_id, "assistant", "原始回答")

        chat_service.edit_user_message(user_msg.id, "修改后的问题")

        user_events = _events("chat_message", str(user_msg.id))
        self.assertEqual([e["op"] for e in user_events], ["create", "edit"])
        self.assertEqual(user_events[1]["content_snapshot"], "修改后的问题")

        # the subsequent assistant message is cascade-deleted -> delete event
        assistant_events = _events("chat_message", str(assistant_msg.id))
        self.assertEqual([e["op"] for e in assistant_events], ["create", "delete"])
        self.assertEqual(assistant_events[-1]["author"], "assistant")

    def test_chat_edit_enqueues_reconcile_and_preserves_deleted_user_role(self) -> None:
        thread_id = self._seed_thread()
        first = chat_service._append_message(thread_id, "user", "第一句")
        chat_service._append_message(thread_id, "assistant", "回答")
        later_user = chat_service._append_message(thread_id, "user", "后续问题")

        with patch.dict(os.environ, {memory_read.WRITE_MODE_ENV: "reconcile"}):
            chat_service.edit_user_message(first.id, "第一句修改")

        later_events = _events("chat_message", str(later_user.id))
        self.assertEqual(later_events[-1]["op"], "delete")
        self.assertEqual(later_events[-1]["author"], "user")
        self.assertEqual(
            [row["type"] for row in db.query_all("SELECT type FROM jobs")],
            [job_service.TYPE_RUN_MEMORY_RECONCILE],
        )


if __name__ == "__main__":
    unittest.main()
