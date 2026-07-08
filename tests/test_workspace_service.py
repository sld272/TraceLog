from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, soul_service, workspace_service


class WorkspaceServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        soul_service.SOULS_DIR = self.workspace / "souls"

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        soul_service.SOULS_DIR = self.old_souls_dir
        self.tmp.cleanup()

    def test_init_workspace_creates_db_and_souls(self) -> None:
        workspace_service.init_workspace()
        self.assertTrue(db.DB_PATH.exists())
        self.assertGreater(len(soul_service.list_souls()), 0)


if __name__ == "__main__":
    unittest.main()
