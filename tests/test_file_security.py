from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import file_security


class FileSecurityTest(unittest.TestCase):
    def test_windows_directory_uses_one_protected_inheritable_acl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private"
            path.mkdir()
            with (
                patch("core.file_security._is_windows", return_value=True),
                patch.dict(
                    os.environ,
                    {"USERDOMAIN": "TESTDOMAIN", "USERNAME": "test-user"},
                    clear=False,
                ),
                patch("core.file_security.subprocess.run") as run,
            ):
                file_security.make_private(path)

        command = run.call_args.args[0]
        self.assertEqual(
            [
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                r"TESTDOMAIN\test-user:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
                "/Q",
            ],
            command,
        )
        self.assertTrue(run.call_args.kwargs["check"])

    def test_windows_command_failure_is_exposed_as_os_error(self) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            with (
                patch("core.file_security._is_windows", return_value=True),
                patch(
                    "core.file_security.subprocess.run",
                    side_effect=subprocess.CalledProcessError(
                        5,
                        ["icacls.exe"],
                        stderr="Access is denied.",
                    ),
                ),
            ):
                with self.assertRaisesRegex(OSError, "Access is denied"):
                    file_security.make_private(handle.name)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_posix_uses_owner_only_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "private"
            directory.mkdir(mode=0o755)
            file = directory / "secret"
            file.write_text("secret", encoding="utf-8")
            file.chmod(0o644)

            file_security.make_private(directory)
            file_security.make_private(file)

            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(file.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
