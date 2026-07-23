from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import paths


class PathsTest(unittest.TestCase):
    def test_dev_uses_project_root_for_resources_and_data(self) -> None:
        with (
            patch.object(sys, "frozen", False, create=True),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop(paths.DATA_DIR_ENV, None)
            self.assertEqual(paths.PROJECT_ROOT, paths.resource_dir())
            self.assertEqual(paths.PROJECT_ROOT, paths.data_dir())

    def test_data_dir_override_wins_in_dev_and_frozen_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "desktop-data"
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.dict(os.environ, {paths.DATA_DIR_ENV: str(override)}),
            ):
                self.assertEqual(override.resolve(), paths.data_dir())

    def test_frozen_macos_uses_application_support(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "platform", "darwin"),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop(paths.DATA_DIR_ENV, None)
            self.assertEqual(
                Path.home() / "Library" / "Application Support" / "TraceLog",
                paths.data_dir(),
            )

    def test_frozen_resources_use_pyinstaller_bundle_directory(self) -> None:
        bundle_dir = Path("/tmp/tracelog-pyinstaller")
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(bundle_dir), create=True),
        ):
            self.assertEqual(bundle_dir, paths.resource_dir())


if __name__ == "__main__":
    unittest.main()
