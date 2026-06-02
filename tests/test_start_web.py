import argparse
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from core.web import app as web_app


class StartWebTest(unittest.TestCase):
    def test_backend_command_uses_current_python_without_conda_wrapper(self) -> None:
        args = argparse.Namespace(host="127.0.0.1", backend_port=8000)

        command = web_app._backend_command(args)

        self.assertEqual(sys.executable, command[0])
        self.assertEqual(["-m", "uvicorn", "api.app:app"], command[1:4])
        self.assertNotIn("conda", command)
        self.assertNotIn("--reload", command)
        self.assertIn("8000", command)

    def test_frontend_command_uses_node_vite_entrypoint_without_npm_script(self) -> None:
        args = argparse.Namespace(host="127.0.0.1", frontend_port=5173)
        node = "node"

        command = web_app._frontend_command(args, node)

        self.assertEqual(node, command[0])
        self.assertEqual(str(web_app.VITE_ENTRYPOINT), command[1])
        self.assertNotIn("npm", command)
        self.assertNotIn("run", command)
        self.assertIn("--host", command)
        self.assertIn("127.0.0.1", command)
        self.assertIn("--port", command)
        self.assertIn("5173", command)

    def test_posix_stop_sends_sigint_before_sigterm(self) -> None:
        process = Mock()
        process.pid = 12345
        process.wait.return_value = 0

        with (
            patch("core.web.app.os.getpgid", return_value=54321, create=True),
            patch("core.web.app.os.killpg", create=True) as killpg,
        ):
            web_app._stop_posix_process_group(process)

        killpg.assert_called_once_with(54321, signal.SIGINT)
        process.wait.assert_called_once_with(timeout=8)

    def test_windows_stop_uses_ctrl_break_when_available(self) -> None:
        process = Mock()
        process.wait.return_value = 0
        ctrl_break = 1

        with patch("core.web.app.signal.CTRL_BREAK_EVENT", ctrl_break, create=True):
            web_app._stop_windows_process(process)

        process.send_signal.assert_called_once_with(ctrl_break)
        process.wait.assert_called_once_with(timeout=8)

    def test_windows_stop_kills_process_tree_after_ctrl_break_timeout(self) -> None:
        process = Mock()
        process.pid = 12345
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="server", timeout=8),
            0,
        ]

        with (
            patch("core.web.app.signal.CTRL_BREAK_EVENT", 1, create=True),
            patch("core.web.app.subprocess.run") as run,
        ):
            web_app._stop_windows_process(process)

        process.send_signal.assert_called_once_with(1)
        run.assert_called_once_with(
            ["taskkill", "/PID", "12345", "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
