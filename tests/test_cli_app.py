from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

openai_stub = ModuleType("openai")
setattr(openai_stub, "OpenAI", lambda **kwargs: SimpleNamespace(kwargs=kwargs))
sys.modules.setdefault("openai", openai_stub)

from core.cli import app as cli_app
from core import db


class CliAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_cli_startup_reconciles_and_flushes_vector_index_before_loop(self) -> None:
        config = {
            "api_key": "main-key",
            "base_url": "https://api.openai.com/v1",
            "model": "chat-model",
            "embedding_model": "embedding-model",
            "embedding_api_key": None,
            "embedding_base_url": None,
            "logging": {"enabled": False},
        }
        vector_result = SimpleNamespace(collection_name="tracelog_abcd1234", indexed_count=0, path="/tmp/chroma")

        with (
            patch("core.cli.app.load_config", return_value=config),
            patch("core.cli.app.workspace_service.migrate_workspace_permissions") as migrate_permissions,
            patch("core.cli.app.workspace_service.init_workspace"),
            patch("core.cli.app.vectorstore.init_vectorstore", return_value=vector_result),
            patch("core.cli.app.vectorstore.current_embedding_config_hash", return_value="hash"),
            patch("core.cli.app.record_service.reindex_all_vector_docs", return_value=3) as reindex,
            patch("core.cli.app.record_service.retry_pending_vector_docs", return_value=3) as retry_pending,
            patch("core.cli.app.read_cli_input", side_effect=KeyboardInterrupt),
            patch("core.cli.app.sessions.run_memory_reconcile"),
            redirect_stdout(StringIO()),
        ):
            cli_app.main()

        reindex.assert_called_once_with()
        retry_pending.assert_called_once_with()
        migrate_permissions.assert_called_once_with()

    def test_cli_startup_reconciles_even_when_collection_has_existing_count(self) -> None:
        config = {
            "api_key": "main-key",
            "base_url": "https://api.openai.com/v1",
            "model": "chat-model",
            "embedding_model": "embedding-model",
            "embedding_api_key": None,
            "embedding_base_url": None,
            "logging": {"enabled": False},
        }
        vector_result = SimpleNamespace(collection_name="tracelog_abcd1234", indexed_count=2, path="/tmp/chroma")

        with (
            patch("core.cli.app.load_config", return_value=config),
            patch("core.cli.app.workspace_service.migrate_workspace_permissions") as migrate_permissions,
            patch("core.cli.app.workspace_service.init_workspace"),
            patch("core.cli.app.vectorstore.init_vectorstore", return_value=vector_result),
            patch("core.cli.app.vectorstore.current_embedding_config_hash", return_value="hash"),
            patch("core.cli.app.record_service.reindex_all_vector_docs", return_value=0) as reindex,
            patch("core.cli.app.record_service.retry_pending_vector_docs", return_value=1) as retry_pending,
            patch("core.cli.app.read_cli_input", side_effect=KeyboardInterrupt),
            patch("core.cli.app.sessions.run_memory_reconcile"),
            redirect_stdout(StringIO()),
        ):
            cli_app.main()

        reindex.assert_called_once_with()
        retry_pending.assert_called_once_with()
        migrate_permissions.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
