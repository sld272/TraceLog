from __future__ import annotations

import tempfile
import unittest
import io
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from core import attachment_service, db, reply_service, vector_index_service, vision_service
from core.app_services import event_service, job_service, public_post_pipeline
from tests.helpers import require_not_none


class FakeVectorStore:
    def __init__(self) -> None:
        self.indexed: list[str] = []

    def is_initialized(self) -> bool:
        return True

    def current_collection_name(self) -> str:
        return "tracelog_test"

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        self.indexed.extend(texts)
        return [np.asarray([1.0, 0.0], dtype=np.float32) for _ in texts]

    def delete_documents(self, doc_ids: list[str]) -> None:
        del doc_ids


class PublicPostPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_create_post_returns_immediately_and_enqueues_jobs(self) -> None:
        created = public_post_pipeline.create_post("今天想练歌")
        jobs = db.query_all("SELECT type FROM jobs ORDER BY id ASC")
        events = event_service.list_post_events(created.post_id)

        self.assertEqual(created.post_id, require_not_none(db.query_one("SELECT id FROM posts"))["id"])
        self.assertEqual(
            [
                "index_post_embedding",
                "generate_post_replies",
                "run_memory_reconcile",
            ],
            [row["type"] for row in jobs],
        )
        self.assertEqual([job["id"] for job in db.query_all("SELECT id FROM jobs ORDER BY id ASC")], created.job_ids)
        self.assertEqual(["post_created"], [event["event_type"] for event in events])

    def test_image_only_post_enqueues_reply_without_text_indexing_jobs(self) -> None:
        attachment = attachment_service.upload_image(_image_bytes(), content_type="image/png")

        created = public_post_pipeline.create_post("", [attachment.id])
        jobs = job_service.list_jobs_for_post(created.post_id)

        self.assertEqual(
            ["generate_post_replies", "run_memory_reconcile"],
            [job["type"] for job in jobs],
        )

    def test_image_summary_becomes_post_vision_evidence(self) -> None:
        attachment = attachment_service.upload_image(
            _image_bytes(), content_type="image/png"
        )
        created = public_post_pipeline.create_post("", [attachment.id])
        summary = vision_service.VisionSummary(
            attachment_id=attachment.id,
            status="ok",
            description="图片里是一块学习计划看板。",
            visible_text=["考研计划"],
            uncertainties=[],
        )
        public_context = public_post_pipeline.PublicPostReplyContext(
            llm_content="图片里是一块学习计划看板。",
            built_context=SimpleNamespace(enabled_souls=[]),
        )
        with (
            patch(
                "core.app_services.public_post_pipeline.vision_service.describe_attachments",
                return_value=[summary],
            ),
            patch(
                "core.app_services.public_post_pipeline.record_service.index_post_vision_embedding"
            ),
            patch(
                "core.app_services.public_post_pipeline.build_public_post_reply_context",
                return_value=public_context,
            ),
        ):
            public_post_pipeline._run_generate_post_replies(
                1,
                {"post_id": created.post_id},
                client=object(),
                model="m",
            )

        event = require_not_none(
            db.query_one(
                """
                SELECT * FROM memory_ingest_events
                WHERE source_type = 'post_vision' AND source_id = ?
                """,
                (created.post_id,),
            )
        )
        self.assertIn("学习计划看板", event["content_snapshot"])

    def test_index_post_embedding_job_indexes_and_emits_events(self) -> None:
        fake_vectorstore = FakeVectorStore()
        created = public_post_pipeline.create_post("今天想练歌")
        job = require_not_none(job_service.claim_next_pending())

        with (
            patch("core.vectorstore.is_initialized", fake_vectorstore.is_initialized),
            patch("core.vectorstore.current_collection_name", fake_vectorstore.current_collection_name),
            patch("core.vectorstore.embed_texts", fake_vectorstore.embed_texts),
            patch("core.vectorstore.delete_documents", fake_vectorstore.delete_documents),
        ):
            public_post_pipeline.execute_job(job, client=None, model="fake")  # type: ignore[arg-type]

        self.assertEqual(["今天想练歌"], fake_vectorstore.indexed)
        self.assertEqual(
            ["post_created", "embedding_started", "embedding_succeeded"],
            [event["event_type"] for event in event_service.list_post_events(created.post_id)],
        )

    def test_reply_job_without_souls_emits_no_reply_success(self) -> None:
        created = public_post_pipeline.create_post("今天想练歌")
        first = require_not_none(job_service.claim_next_pending())
        job_service.mark_succeeded(first["id"])
        job = require_not_none(job_service.claim_next_pending())

        public_post_pipeline.execute_job(job, client=None, model="fake")  # type: ignore[arg-type]
        event_types = [event["event_type"] for event in event_service.list_post_events(created.post_id)]

        self.assertIn("reply_started", event_types)
        self.assertIn("reply_succeeded", event_types)

    def test_public_reply_event_and_root_metadata_receive_inline_suggestion(self) -> None:
        created = public_post_pipeline.create_post("我决定准备考研")
        job_id = require_not_none(
            db.query_one(
                "SELECT id FROM jobs WHERE type = ? AND json_extract(payload_json, '$.post_id') = ?",
                (job_service.TYPE_GENERATE_POST_REPLIES, created.post_id),
            )
        )["id"]
        built_context = SimpleNamespace(
            enabled_souls=[SimpleNamespace(name="拾迹者")],
        )
        public_context = public_post_pipeline.PublicPostReplyContext(
            llm_content="我决定准备考研",
            built_context=built_context,
        )
        suggestion = {
            "id": "s_1",
            "kind": "goal",
            "payload": {"title": "准备考研", "horizon": "long"},
            "evidence_ref": f"post:{created.post_id}",
            "confidence": 0.9,
            "status": "pending",
            "normalized_key": "sha256:x",
            "created_at": 1.0,
            "decided_at": None,
        }
        result = reply_service.SoulReplyResult(
            soul_name="拾迹者",
            sort_order=0,
            ok=True,
            reply="认真规划。",
            error=None,
        )

        with (
            patch(
                "core.app_services.public_post_pipeline.build_public_post_reply_context",
                return_value=public_context,
            ),
            patch("core.app_services.public_post_pipeline.reply_service.fanout", return_value=[result]),
            patch(
                "core.app_services.public_post_pipeline.suggestion_pipeline.collect_reply_suggestions",
                return_value=[suggestion],
            ),
            patch(
                "core.app_services.public_post_pipeline.reply_service.attach_suggestions_to_root_comment"
            ) as attach,
        ):
            public_post_pipeline._run_generate_post_replies(
                int(job_id),
                {"post_id": created.post_id},
                client=object(),
                model="m",
            )

        reply_event = [
            event
            for event in event_service.list_post_events(created.post_id)
            if event["event_type"] == "reply_succeeded" and event["payload"].get("soul_name")
        ][-1]
        self.assertEqual([suggestion], reply_event["payload"]["suggestions"])
        attach.assert_called_once_with(created.post_id, "拾迹者", [suggestion])

    def test_reply_job_fails_when_soul_reply_fails(self) -> None:
        db.execute(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            ("拾迹者", "souls/拾迹者.md", 1.0, 1.0),
        )
        soul_dir = self.workspace / "souls"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "拾迹者.md").write_text("拾迹者人格", encoding="utf-8")
        created = public_post_pipeline.create_post("今天想练歌")
        first = require_not_none(job_service.claim_next_pending())
        job_service.mark_succeeded(first["id"])
        job = require_not_none(job_service.claim_next_pending())

        with self.assertRaisesRegex(RuntimeError, "reply generation failed"):
            public_post_pipeline.execute_job(job, client=None, model="bad-model")  # type: ignore[arg-type]

        event_types = [event["event_type"] for event in event_service.list_post_events(created.post_id)]
        self.assertIn("reply_failed", event_types)
        self.assertIsNone(
            db.query_one(
                "SELECT content FROM comments WHERE post_id = ? AND soul_name = ?",
                (created.post_id, "拾迹者"),
            )
        )


if __name__ == "__main__":
    unittest.main()


def _image_bytes() -> bytes:
    image = Image.new("RGB", (8, 8), color=(10, 20, 30))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
