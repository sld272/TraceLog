from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, soul_service, workspace_service
from core.cli import config as cli_config


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
        self.config_path = Path(self.tmp.name) / "config.json"

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        soul_service.SOULS_DIR = self.old_souls_dir
        self.tmp.cleanup()

    def test_init_workspace_creates_db_and_souls(self) -> None:
        workspace_service.init_workspace()
        self.assertTrue(db.DB_PATH.exists())
        self.assertGreater(len(soul_service.list_souls()), 0)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_migrate_workspace_permissions_updates_all_targets_and_is_idempotent(self) -> None:
        paths = self._create_public_permission_targets()

        with patch.object(cli_config, "CONFIG_FILE", str(self.config_path)):
            workspace_service.migrate_workspace_permissions()
            workspace_service.migrate_workspace_permissions()

        self.assertEqual(0o700, self._mode(self.workspace))
        self.assertEqual(0o600, self._mode(paths["db"]))
        self.assertEqual(0o600, self._mode(paths["wal"]))
        self.assertEqual(0o600, self._mode(paths["shm"]))
        self.assertEqual(0o700, self._mode(paths["chroma"]))
        self.assertEqual(0o600, self._mode(self.config_path))

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_migrate_workspace_permissions_continues_after_one_chmod_failure(self) -> None:
        paths = self._create_public_permission_targets()
        failed_path = paths["wal"]
        real_chmod = os.chmod

        def chmod_with_failure(path, mode):
            if Path(path) == failed_path:
                raise OSError("unsupported permissions")
            real_chmod(path, mode)

        with (
            patch.object(cli_config, "CONFIG_FILE", str(self.config_path)),
            patch("core.workspace_service.os.chmod", side_effect=chmod_with_failure),
        ):
            workspace_service.migrate_workspace_permissions()

        self.assertEqual(0o700, self._mode(self.workspace))
        self.assertEqual(0o600, self._mode(paths["db"]))
        self.assertEqual(0o644, self._mode(failed_path))
        self.assertEqual(0o600, self._mode(paths["shm"]))
        self.assertEqual(0o700, self._mode(paths["chroma"]))
        self.assertEqual(0o600, self._mode(self.config_path))

    def _create_public_permission_targets(self) -> dict[str, Path]:
        self.workspace.mkdir(parents=True)
        chroma = self.workspace / "chroma_db"
        chroma.mkdir()
        db_path = db.DB_PATH
        wal = Path(f"{db.DB_PATH}-wal")
        shm = Path(f"{db.DB_PATH}-shm")
        for path in (db_path, wal, shm, self.config_path):
            path.write_text("test", encoding="utf-8")

        self.workspace.chmod(0o755)
        chroma.chmod(0o755)
        for path in (db_path, wal, shm, self.config_path):
            path.chmod(0o644)
        return {"db": db_path, "wal": wal, "shm": shm, "chroma": chroma}

    @staticmethod
    def _mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)


if __name__ == "__main__":
    unittest.main()
