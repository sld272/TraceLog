from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    chat_service,
    comment_service,
    db,
    record_service,
)
from core.app_services import post_mutation


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
        self.assertEqual(events[0]["owner_scope"], "global")  # user-authored
        self.assertEqual(events[0]["visibility_scope"], f"thread:{post_id}")

    def test_delete_comment_appends_delete_events(self) -> None:
        post_id = record_service.save_post("帖子", index_immediately=False, track_embedding=False)
        self._seed_root_comment(post_id)
        msg = comment_service.append_comment(post_id, "luna", "user", "要被删的追问")
        comment_service.delete_message(msg.id)
        events = _events("comment_message", str(msg.id))
        self.assertEqual([e["op"] for e in events], ["create", "delete"])

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


if __name__ == "__main__":
    unittest.main()
