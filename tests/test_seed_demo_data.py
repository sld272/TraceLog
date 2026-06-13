from __future__ import annotations

import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path

from core import db, vector_index_service
from core.app_services import job_service
from scripts import seed_demo_data

EXAMPLE_PATH = seed_demo_data.SCRIPT_DIR / seed_demo_data.EXAMPLE_FILENAME


def _write_toml(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
    handle.write(textwrap.dedent(text))
    handle.close()
    return Path(handle.name)


class SeedDemoContentTest(unittest.TestCase):
    def test_example_content_loads_and_is_consistent(self) -> None:
        content = seed_demo_data.load_content(EXAMPLE_PATH)

        keys = [post.key for post in content.posts]
        self.assertGreaterEqual(len(keys), 1)
        self.assertEqual(len(keys), len(set(keys)))  # key 无重复
        key_set = set(keys)
        for thread in content.comment_threads:
            self.assertIn(thread.post_key, key_set)  # 追问都能绑定到帖子

        blob = "\n".join(post.content for post in content.posts)
        for marker in ("仙林", "法语", "跨考计算机", "四级", "打了两小时球", "番"):
            self.assertIn(marker, blob)

    def test_build_demo_plan_orders_by_time_and_limits(self) -> None:
        content = seed_demo_data.load_content(EXAMPLE_PATH)

        plan = seed_demo_data.build_demo_plan(content.posts, year=2026, limit=4)

        self.assertEqual(4, len(plan))
        times = [item.created_at for item in plan]
        self.assertEqual(times, sorted(times))
        self.assertEqual("2026-03-16T21:40:00+08:00", plan[0].created_at.isoformat())

    def test_resolve_content_path_returns_existing_file(self) -> None:
        path = seed_demo_data.resolve_content_path(None)

        self.assertTrue(path.exists())
        self.assertIn(path.name, {seed_demo_data.CONTENT_FILENAME, seed_demo_data.EXAMPLE_FILENAME})

    def test_load_content_rejects_duplicate_keys(self) -> None:
        path = _write_toml(
            """
            [[posts]]
            key = "a"
            month = 3
            day = 1
            hour = 9
            minute = 0
            act = "x"
            content = "一"

            [[posts]]
            key = "a"
            month = 3
            day = 2
            hour = 9
            minute = 0
            act = "x"
            content = "二"
            """
        )
        self.addCleanup(path.unlink)

        with self.assertRaises(ValueError):
            seed_demo_data.load_content(path)

    def test_load_content_rejects_orphan_post_key(self) -> None:
        path = _write_toml(
            """
            [[posts]]
            key = "real"
            month = 3
            day = 1
            hour = 9
            minute = 0
            act = "x"
            content = "一"

            [[comment_threads]]
            post_key = "ghost"
            soul_name = "拾迹者"
            followup = "?"
            """
        )
        self.addCleanup(path.unlink)

        with self.assertRaises(ValueError):
            seed_demo_data.load_content(path)

    def test_cap_returns_all_items_for_negative_count(self) -> None:
        items = [1, 2, 3]

        self.assertEqual(items, seed_demo_data._cap(items, -1))
        self.assertEqual([1], seed_demo_data._cap(items, 1))


class SeedDemoDataJobTest(unittest.TestCase):
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

    def test_create_demo_post_only_enqueues_global_reflection_when_requested(self) -> None:
        when = datetime(2026, 3, 16, 21, 40).astimezone()

        first = seed_demo_data.create_demo_post("第一条", created_at=when, trigger_global_deep_reflection=False)
        first_job_types = [job["type"] for job in job_service.list_jobs_for_post(first.post_id)]
        all_job_types = [job["type"] for job in job_service.list_jobs(limit=20)]

        self.assertIn(job_service.TYPE_RUN_LIGHT_REFLECTION, first_job_types)
        self.assertNotIn(job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION, all_job_types)

        seed_demo_data.create_demo_post("第三条", created_at=when, trigger_global_deep_reflection=True)
        all_job_types = [job["type"] for job in job_service.list_jobs(limit=20)]

        self.assertIn(job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION, all_job_types)

    def test_note_soul_round_enqueues_reflection_every_interval(self) -> None:
        rounds: dict[str, int] = {}

        # 前两轮不触发
        self.assertIsNone(seed_demo_data.note_soul_round_and_maybe_reflect("拾迹者", rounds))
        self.assertIsNone(seed_demo_data.note_soul_round_and_maybe_reflect("拾迹者", rounds))

        # 第三轮触发，且只针对该人格
        job_id = seed_demo_data.note_soul_round_and_maybe_reflect("拾迹者", rounds)
        self.assertIsNotNone(job_id)
        job = job_service.get_job(int(job_id))
        self.assertIsNotNone(job)
        self.assertEqual(job_service.TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS, job["type"])
        self.assertEqual("demo_seed_interval", job["payload"]["trigger"])
        self.assertEqual(["拾迹者"], job["payload"]["soul_names"])

        # 另一个人格独立计数，不受影响
        self.assertIsNone(seed_demo_data.note_soul_round_and_maybe_reflect("毒舌好友", rounds))


if __name__ == "__main__":
    unittest.main()
