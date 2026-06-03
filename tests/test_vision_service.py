from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from core import attachment_service, db, logging_service, vision_service


class VisionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config_path = Path(self.tmp.name) / "config.json"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_config_file = vision_service.CONFIG_FILE
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        vision_service.CONFIG_FILE = str(self.config_path)
        db.init_db()
        logging_service.init_logging({"enabled": True})

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        vision_service.CONFIG_FILE = self.old_config_file
        self.tmp.cleanup()

    def test_describe_attachments_calls_vision_llm_and_caches_summary(self) -> None:
        attachment = attachment_service.upload_image(_image_bytes(), content_type="image/png")
        self.config_path.write_text(
            json.dumps(
                {
                    "api_key": "main-key",
                    "base_url": "https://main.invalid/v1",
                    "vision": {
                        "enabled": True,
                        "model": "vision-model",
                        "api_key": "vision-key",
                        "base_url": "https://vision.invalid/v1",
                    },
                }
            ),
            encoding="utf-8",
        )
        fake_openai = _fake_openai_module(
            json.dumps(
                {
                    "images": [
                        {
                            "attachment_id": attachment.id,
                            "description": "一张用于测试的图片。",
                            "visible_text": ["TEST"],
                            "uncertainties": [],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

        with patch.dict(sys.modules, {"openai": fake_openai}):
            summaries = vision_service.describe_attachments([attachment])

        self.assertEqual(1, len(summaries))
        self.assertEqual("一张用于测试的图片。", summaries[0].description)
        self.assertIn("一张用于测试的图片", vision_service.content_with_cached_summaries("", [attachment]))
        row = db.query_one("SELECT status, description FROM vision_cache WHERE attachment_id = ?", (attachment.id,))
        self.assertIsNotNone(row)
        self.assertEqual("ok", row["status"])

    def test_logging_redacts_image_data_urls(self) -> None:
        logging_service.log_llm_call(
            call_id="call-1",
            operation="vision",
            model="vision-model",
            status="ok",
            duration_ms=1,
            timeout_s=60,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,abc123"},
                        }
                    ],
                }
            ],
        )

        serialized = json.dumps(self._last_record(), ensure_ascii=False)
        self.assertNotIn("abc123", serialized)
        self.assertIn("[REDACTED_IMAGE_DATA_URL]", serialized)

    def _last_record(self) -> dict:
        current = self.workspace / "logs" / "current.jsonl"
        lines = [line for line in current.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(lines)
        return json.loads(lines[-1])


def _image_bytes() -> bytes:
    image = Image.new("RGB", (8, 8), color=(10, 20, 30))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _fake_openai_module(response_content: str):
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=response_content))]
            )

    return types.SimpleNamespace(OpenAI=FakeOpenAI)


if __name__ == "__main__":
    unittest.main()
