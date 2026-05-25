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
                }
            ),
            encoding="utf-8",
        )

        loaded = cli_config.load_config()

        self.assertEqual("key", loaded["api_key"])
        self.assertIsNone(loaded["embedding_api_key"])
        self.assertIsNone(loaded["embedding_base_url"])


if __name__ == "__main__":
    unittest.main()
