from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

openai_stub = ModuleType("openai")
setattr(openai_stub, "OpenAI", lambda **kwargs: SimpleNamespace(kwargs=kwargs))
sys.modules.setdefault("openai", openai_stub)

from core.cli import app as cli_app


class CliAppTest(unittest.TestCase):
    def test_empty_vector_collection_triggers_reindex_before_loop(self) -> None:
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
            patch("core.cli.app.workspace_service.init_workspace"),
            patch("core.cli.app.vectorstore.init_vectorstore", return_value=vector_result),
            patch("core.cli.app.record_service.reindex_all_vector_docs", return_value=3) as reindex,
            patch("core.cli.app.record_service.retry_pending_vector_docs") as retry_pending,
            patch("core.cli.app.todo_service.load_todos", return_value=[]),
            patch("core.cli.app.tool_config_service.is_tool_enabled", return_value=False),
            patch("core.cli.app.read_cli_input", side_effect=KeyboardInterrupt),
            patch("core.cli.app.sessions.run_deep_reflection_on_exit"),
            redirect_stdout(StringIO()),
        ):
            cli_app.main()

        reindex.assert_called_once_with()
        retry_pending.assert_not_called()

    def test_non_empty_vector_collection_retries_pending_without_full_reindex(self) -> None:
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
            patch("core.cli.app.workspace_service.init_workspace"),
            patch("core.cli.app.vectorstore.init_vectorstore", return_value=vector_result),
            patch("core.cli.app.record_service.reindex_all_vector_docs") as reindex,
            patch("core.cli.app.record_service.retry_pending_vector_docs", return_value=1) as retry_pending,
            patch("core.cli.app.todo_service.load_todos", return_value=[]),
            patch("core.cli.app.tool_config_service.is_tool_enabled", return_value=False),
            patch("core.cli.app.read_cli_input", side_effect=KeyboardInterrupt),
            patch("core.cli.app.sessions.run_deep_reflection_on_exit"),
            redirect_stdout(StringIO()),
        ):
            cli_app.main()

        reindex.assert_not_called()
        retry_pending.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
