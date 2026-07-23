from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import db, logging_service
from core.llm.common import call_json_completion


class FakeClient:
    def __init__(self, content: str | None = '{"ok": true}', error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        del kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


class LoggingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        db.WORKSPACE_DIR = self.workspace

    def tearDown(self) -> None:
        logging_service.update_config({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        self.tmp.cleanup()

    def test_init_logging_creates_current_log_and_rotates_previous_current(self) -> None:
        logging_service.init_logging({"enabled": True})
        current = self.workspace / "logs" / "current.jsonl"
        current.write_text('{"event":"old"}\n', encoding="utf-8")

        logging_service.init_logging({"enabled": True})

        history = list((self.workspace / "logs" / "history").glob("*.jsonl"))
        self.assertTrue(current.exists())
        self.assertEqual("", current.read_text(encoding="utf-8"))
        self.assertEqual(1, len(history))
        self.assertIn('"old"', history[0].read_text(encoding="utf-8"))

    def test_normalize_config_ignores_old_key_and_clamps_budgets(self) -> None:
        normalized = logging_service.normalize_config(
            {
                "enabled": 0,
                "capture_content": 0,
                "history_retention": 2,
                "rotate_max_bytes": 1,
                "history_max_bytes": 10**20,
                "history_max_days": 0,
            }
        )

        self.assertFalse(normalized["enabled"])
        self.assertFalse(normalized["capture_content"])
        self.assertNotIn("history_retention", normalized)
        self.assertEqual(1024 * 1024, normalized["rotate_max_bytes"])
        self.assertEqual(1024 * 1024 * 1024, normalized["history_max_bytes"])
        self.assertEqual(1, normalized["history_max_days"])

        upper = logging_service.normalize_config(
            {"rotate_max_bytes": 10**20, "history_max_bytes": 1, "history_max_days": 999}
        )
        self.assertEqual(100 * 1024 * 1024, upper["rotate_max_bytes"])
        self.assertEqual(10 * 1024 * 1024, upper["history_max_bytes"])
        self.assertEqual(365, upper["history_max_days"])

    def test_content_capture_defaults_to_off(self) -> None:
        self.assertFalse(logging_service.default_config()["capture_content"])
        self.assertFalse(logging_service.normalize_config({})["capture_content"])

    def test_log_event_redacts_sensitive_values(self) -> None:
        logging_service.init_logging({"enabled": True})

        logging_service.log_event(
            "secret_probe",
            api_key="sk-thisShouldNotAppear123456",
            nested={"authorization": "Bearer secretShouldNotAppear123456"},
            text="token sk-thisShouldNotAppear123456 in body",
        )

        record = self._last_record()
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("thisShouldNotAppear", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_all_string_leaves_are_truncated(self) -> None:
        logging_service.init_logging({"enabled": True})
        logging_service.log_event("truncate_probe", nested={"text": "x" * 16_025})

        text = self._last_record()["nested"]["text"]
        self.assertTrue(text.startswith("x" * 16_000))
        self.assertTrue(text.endswith("…[truncated 25 chars]"))

    def test_redaction_happens_before_truncation(self) -> None:
        logging_service.init_logging({"enabled": True})
        with patch.object(logging_service, "MAX_STRING_LENGTH", 10):
            logging_service.log_event("order_probe", api_key="sk-secretValue123456789")

        self.assertEqual("[REDACTED]", self._last_record()["api_key"])

    def test_llm_content_capture_on_includes_content_and_telemetry(self) -> None:
        messages = [{"role": "user", "content": "hello full payload"}]
        logging_service.init_logging({"enabled": True, "capture_content": True})

        result = call_json_completion(
            client=FakeClient('{"ok": true}'),
            model="fake-model",
            operation="full_probe",
            messages=messages,
            parser=lambda content: json.loads(content or "{}"),
            response_format={"type": "json_object"},
        )

        self.assertEqual({"ok": True}, result)
        record = self._last_record()
        self.assertEqual(messages, record["request"]["messages"])
        self.assertEqual('{"ok": true}', record["response"]["content"])
        self.assertEqual({"ok": True}, record["parsed"])
        self.assertEqual({"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}, record["usage"])
        self.assertEqual("stop", record["finish_reason"])
        self.assertEqual({"type": "json_object"}, record["request"]["response_format"])

    def test_llm_content_capture_off_keeps_only_lengths_for_success(self) -> None:
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "hello"},
        ]
        logging_service.init_logging({"enabled": True, "capture_content": False})

        logging_service.log_llm_call(
            call_id="call-1",
            operation="gate_probe",
            model="fake-model",
            status="ok",
            duration_ms=8,
            timeout_s=30,
            messages=messages,
            response_content=" answer ",
            parsed={"private": "value"},
        )

        record = self._last_record()
        self.assertNotIn("messages", record["request"])
        self.assertEqual(2, record["request"]["messages_count"])
        self.assertEqual(10, record["request"]["messages_content_length"])
        self.assertNotIn("content", record["response"])
        self.assertEqual(8, record["response"]["content_length"])
        self.assertEqual(6, record["response"]["content_stripped_length"])
        self.assertNotIn("parsed", record)
        self.assertIn("usage", record)
        self.assertIn("finish_reason", record)

    def test_failed_llm_call_keeps_and_truncates_content_when_capture_is_off(self) -> None:
        logging_service.init_logging({"enabled": True, "capture_content": False})

        logging_service.log_llm_call(
            call_id="call-2",
            operation="failure_probe",
            model="fake-model",
            status="api_error",
            duration_ms=8,
            timeout_s=30,
            messages=[{"role": "user", "content": "m" * 16_010}],
            response_content="r" * 16_020,
            error={"exception_type": "RuntimeError"},
        )

        record = self._last_record()
        self.assertIn("messages", record["request"])
        self.assertTrue(record["request"]["messages"][0]["content"].endswith("…[truncated 10 chars]"))
        self.assertTrue(record["response"]["content"].endswith("…[truncated 20 chars]"))

    def test_llm_helper_logs_statuses_without_changing_return_contract(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        logging_service.init_logging({"enabled": True})

        invalid_json = call_json_completion(
            client=FakeClient("not json"),
            model="fake-model",
            operation="invalid_json_probe",
            messages=messages,
            parser=lambda content: None,
        )
        self.assertIsNone(invalid_json)
        self.assertEqual("invalid_json", self._last_record()["status"])

        api_error = call_json_completion(
            client=FakeClient(error=RuntimeError("boom")),
            model="fake-model",
            operation="api_error_probe",
            messages=messages,
            parser=lambda content: json.loads(content or "{}"),
        )
        api_error_record = self._last_record()
        self.assertIsNone(api_error)
        self.assertEqual("api_error", api_error_record["status"])
        self.assertEqual("RuntimeError", api_error_record["error"]["exception_type"])
        self.assertEqual("boom", api_error_record["error"]["exception_message"])

    def test_write_path_rotates_after_crossing_size_threshold(self) -> None:
        logging_service.init_logging({"enabled": True})
        with patch.object(logging_service, "_rotate_max_bytes", 200):
            logging_service.log_event("rotation_probe", text="x" * 500)

        current = self.workspace / "logs" / "current.jsonl"
        history = list((self.workspace / "logs" / "history").glob("*.jsonl"))
        self.assertEqual("", current.read_text(encoding="utf-8"))
        self.assertEqual(1, len(history))
        self.assertIn("rotation_probe", history[0].read_text(encoding="utf-8"))

    def test_history_pruning_removes_expired_files_first(self) -> None:
        history_dir = self.workspace / "logs" / "history"
        history_dir.mkdir(parents=True)
        expired = history_dir / "expired.jsonl"
        recent = history_dir / "recent.jsonl"
        expired.write_text("old", encoding="utf-8")
        recent.write_text("new", encoding="utf-8")
        old_time = time.time() - 3 * 24 * 60 * 60
        os.utime(expired, (old_time, old_time))

        logging_service._prune_history(history_dir, max_days=1, max_bytes=100)

        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())

    def test_history_pruning_enforces_total_budget_oldest_first(self) -> None:
        history_dir = self.workspace / "logs" / "history"
        history_dir.mkdir(parents=True)
        oldest = history_dir / "oldest.jsonl"
        newest = history_dir / "newest.jsonl"
        oldest.write_text("a" * 6, encoding="utf-8")
        newest.write_text("b" * 6, encoding="utf-8")
        now = time.time()
        os.utime(oldest, (now - 10, now - 10))
        os.utime(newest, (now, now))

        logging_service._prune_history(history_dir, max_days=1, max_bytes=8)

        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())

    def test_update_config_does_not_rotate_or_archive(self) -> None:
        logging_service.init_logging({"enabled": True, "capture_content": True})
        logging_service.log_event("before_update")

        logging_service.update_config({"enabled": True, "capture_content": False})

        history = list((self.workspace / "logs" / "history").glob("*.jsonl"))
        self.assertEqual([], history)
        self.assertIn("before_update", (self.workspace / "logs" / "current.jsonl").read_text(encoding="utf-8"))
        self.assertFalse(logging_service.get_log_stats()["capture_content"])

    def test_logging_can_be_hot_enabled_after_disabled_startup(self) -> None:
        logging_service.init_logging({"enabled": False})
        current = self.workspace / "logs" / "current.jsonl"
        self.assertTrue(current.exists())

        logging_service.update_config({"enabled": True})
        logging_service.log_event("hot_enabled")

        self.assertIn("hot_enabled", current.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits are unavailable")
    def test_init_migrates_directory_and_file_permissions(self) -> None:
        log_dir = self.workspace / "logs"
        history_dir = log_dir / "history"
        history_dir.mkdir(parents=True)
        current = log_dir / "current.jsonl"
        existing_history = history_dir / "existing.jsonl"
        current.write_text("current\n", encoding="utf-8")
        existing_history.write_text("history\n", encoding="utf-8")
        log_dir.chmod(0o755)
        history_dir.chmod(0o755)
        current.chmod(0o644)
        existing_history.chmod(0o644)

        logging_service.init_logging({"enabled": True})

        self.assertEqual(0o700, log_dir.stat().st_mode & 0o777)
        self.assertEqual(0o700, history_dir.stat().st_mode & 0o777)
        for path in [current, *history_dir.glob("*.jsonl")]:
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def _last_record(self) -> dict:
        current = self.workspace / "logs" / "current.jsonl"
        lines = [line for line in current.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(lines)
        return json.loads(lines[-1])


if __name__ == "__main__":
    unittest.main()
