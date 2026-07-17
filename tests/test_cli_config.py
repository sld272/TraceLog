from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
                    "logging": {
                        "llm_payload": "off",
                        "preview_chars": 12,
                        "history_retention": 2,
                        "capture_content": False,
                        "rotate_max_bytes": 1,
                        "history_max_bytes": 10**20,
                        "history_max_days": 999,
                    },
                }
            ),
            encoding="utf-8",
        )

        loaded = cli_config.load_config()

        self.assertEqual("key", loaded["api_key"])
        self.assertIsNone(loaded["embedding_api_key"])
        self.assertIsNone(loaded["embedding_base_url"])
        self.assertIsNone(loaded["secondary_model"])
        self.assertIsNone(loaded["secondary_api_key"])
        self.assertIsNone(loaded["secondary_base_url"])
        self.assertNotIn("llm_payload", loaded["logging"])
        self.assertNotIn("preview_chars", loaded["logging"])
        self.assertNotIn("history_retention", loaded["logging"])
        self.assertFalse(loaded["logging"]["capture_content"])
        self.assertEqual(1024 * 1024, loaded["logging"]["rotate_max_bytes"])
        self.assertEqual(1024 * 1024 * 1024, loaded["logging"]["history_max_bytes"])
        self.assertEqual(365, loaded["logging"]["history_max_days"])
        self.assertEqual(
            {"enabled": False, "model": None, "api_key": None, "base_url": None},
            loaded["vision"],
        )
        self.assertEqual(
            {
                "enabled": False,
                "provider": "duckduckgo",
                "tavily_api_key": None,
                "max_results": 5,
                "timeout_s": 8,
                "cache_ttl_s": 1800,
            },
            loaded["web_search"],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_first_run_writes_config_with_owner_only_permissions(self) -> None:
        with (
            patch.object(cli_config.getpass, "getpass", return_value="sk-secret"),
            patch.object(
                cli_config,
                "read_cli_input",
                side_effect=[
                    "https://example.invalid/v1",
                    "test-model",
                    "test-embedding",
                    "",
                ],
            ),
        ):
            cli_config.load_config()

        mode = stat.S_IMODE(Path(cli_config.CONFIG_FILE).stat().st_mode)
        self.assertEqual(0o600, mode)

    def test_normalize_web_search_config_clamps_values_and_cleans_provider(self) -> None:
        normalized = cli_config.normalize_web_search_config(
            {
                "enabled": True,
                "provider": "bad-provider",
                "tavily_api_key": "  tavily-key  ",
                "max_results": 99,
                "timeout_s": 1,
                "cache_ttl_s": -5,
            }
        )

        self.assertTrue(normalized["enabled"])
        self.assertEqual("duckduckgo", normalized["provider"])
        self.assertEqual("tavily-key", normalized["tavily_api_key"])
        self.assertEqual(8, normalized["max_results"])
        self.assertEqual(3, normalized["timeout_s"])
        self.assertEqual(0, normalized["cache_ttl_s"])
        self.assertNotIn("include_sources", normalized)


if __name__ == "__main__":
    unittest.main()
