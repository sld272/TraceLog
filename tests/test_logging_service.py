from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class LoggingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        db.WORKSPACE_DIR = self.workspace

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        self.tmp.cleanup()

    def test_init_logging_creates_current_log_and_rotates_previous_current(self) -> None:
        logging_service.init_logging({"enabled": True, "history_retention": 5})
        current = self.workspace / "logs" / "current.jsonl"
        current.write_text('{"event":"old"}\n', encoding="utf-8")

        logging_service.init_logging({"enabled": True, "history_retention": 5})

        history = list((self.workspace / "logs" / "history").glob("*.jsonl"))
        self.assertTrue(current.exists())
        self.assertEqual("", current.read_text(encoding="utf-8"))
        self.assertEqual(1, len(history))
        self.assertIn('"old"', history[0].read_text(encoding="utf-8"))

    def test_history_retention_keeps_latest_five_files(self) -> None:
        history_dir = self.workspace / "logs" / "history"
        history_dir.mkdir(parents=True)
        for index in range(7):
            path = history_dir / f"20260527-10000{index}.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            path.touch()

        logging_service.init_logging({"enabled": True, "history_retention": 5})

        history = sorted(path.name for path in history_dir.glob("*.jsonl"))
        self.assertEqual(5, len(history))

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

    def test_llm_payload_modes(self) -> None:
        messages = [{"role": "user", "content": "hello full payload"}]

        logging_service.init_logging({"enabled": True, "llm_payload": "summary", "preview_chars": 5})
        call_json_completion(
            client=FakeClient('{"ok": true}'),
            model="fake-model",
            operation="summary_probe",
            messages=messages,
            parser=lambda content: json.loads(content or "{}"),
        )
        summary = self._last_record()
        self.assertEqual("summary", summary["request"]["mode"])
        self.assertEqual("hello...", summary["request"]["messages"][0]["content_preview"])

        logging_service.init_logging({"enabled": True, "llm_payload": "full"})
        call_json_completion(
            client=FakeClient('{"ok": true}'),
            model="fake-model",
            operation="full_probe",
            messages=messages,
            parser=lambda content: json.loads(content or "{}"),
        )
        full = self._last_record()
        self.assertEqual("full", full["request"]["mode"])
        self.assertEqual(messages, full["request"]["messages"])

        logging_service.init_logging({"enabled": True, "llm_payload": "off"})
        call_json_completion(
            client=FakeClient('{"ok": true}'),
            model="fake-model",
            operation="off_probe",
            messages=messages,
            parser=lambda content: json.loads(content or "{}"),
        )
        off = self._last_record()
        self.assertEqual("off", off["request"]["mode"])
        self.assertEqual("off", off["response"]["mode"])

    def test_llm_helper_logs_statuses_without_changing_return_contract(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        logging_service.init_logging({"enabled": True})

        result = call_json_completion(
            client=FakeClient('{"ok": true}'),
            model="fake-model",
            operation="ok_probe",
            messages=messages,
            parser=lambda content: json.loads(content or "{}"),
        )
        self.assertEqual({"ok": True}, result)
        self.assertEqual("ok", self._last_record()["status"])

        invalid_json = call_json_completion(
            client=FakeClient("not json"),
            model="fake-model",
            operation="invalid_json_probe",
            messages=messages,
            parser=lambda content: None,
        )
        self.assertIsNone(invalid_json)
        self.assertEqual("invalid_json", self._last_record()["status"])

        invalid_response = call_json_completion(
            client=FakeClient("{}"),
            model="fake-model",
            operation="invalid_response_probe",
            messages=messages,
            parser=lambda content: None,
        )
        self.assertIsNone(invalid_response)
        self.assertEqual("invalid_response", self._last_record()["status"])

        api_error = call_json_completion(
            client=FakeClient(error=RuntimeError("boom")),
            model="fake-model",
            operation="api_error_probe",
            messages=messages,
            parser=lambda content: json.loads(content or "{}"),
        )
        self.assertIsNone(api_error)
        self.assertEqual("api_error", self._last_record()["status"])

    def _last_record(self) -> dict:
        current = self.workspace / "logs" / "current.jsonl"
        lines = [line for line in current.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(lines)
        return json.loads(lines[-1])


if __name__ == "__main__":
    unittest.main()
