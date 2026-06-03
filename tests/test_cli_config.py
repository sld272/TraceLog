from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.cli import config as cli_config


class CliConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_load_config_reads_existing_config_and_defaults_optional_embedding_fields(self) -> None:
        Path(cli_config.CONFIG_FILE).write_text(
            json.dumps(
                {
                    "api_key": "key",
                    "base_url": "https://example.invalid/v1",
                    "model": "model",
                    "embedding_model": "embedding",
                    "logging": {"llm_payload": "off", "preview_chars": 12, "history_retention": 2},
                }
            ),
            encoding="utf-8",
        )

        loaded = cli_config.load_config()

        self.assertEqual("key", loaded["api_key"])
        self.assertIsNone(loaded["embedding_api_key"])
        self.assertIsNone(loaded["embedding_base_url"])
        self.assertNotIn("llm_payload", loaded["logging"])
        self.assertNotIn("preview_chars", loaded["logging"])
        self.assertEqual(2, loaded["logging"]["history_retention"])
        self.assertEqual(
            {"enabled": False, "model": None, "api_key": None, "base_url": None},
            loaded["vision"],
        )


if __name__ == "__main__":
    unittest.main()
