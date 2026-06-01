import argparse
import signal
import sys
import unittest
from unittest.mock import Mock, patch

import start_web


class StartWebTest(unittest.TestCase):
    def test_backend_command_uses_current_python_without_conda_wrapper(self) -> None:
        args = argparse.Namespace(host="127.0.0.1", backend_port=8000)

        command = start_web._backend_command(args)

        self.assertEqual(sys.executable, command[0])
        self.assertEqual(["-m", "uvicorn", "api.app:app"], command[1:4])
        self.assertNotIn("conda", command)
        self.assertIn("--reload", command)
        self.assertIn("8000", command)

    def test_posix_stop_sends_sigint_before_sigterm(self) -> None:
        process = Mock()
        process.pid = 12345
        process.wait.return_value = 0

        with (
            patch("start_web.os.getpgid", return_value=54321),
            patch("start_web.os.killpg") as killpg,
        ):
            start_web._stop_posix_process_group(process)

        killpg.assert_called_once_with(54321, signal.SIGINT)
        process.wait.assert_called_once_with(timeout=8)


if __name__ == "__main__":
    unittest.main()
