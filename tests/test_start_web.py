import argparse
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from core.web import app as web_app


class StartWebTest(unittest.TestCase):
    def test_default_mode_serves_production_app(self) -> None:
        with patch("core.web.app._serve", return_value=0) as serve:
            result = web_app.main(["--no-open"])

        self.assertEqual(0, result)
        serve.assert_called_once_with(["--no-open"])

    def test_dev_subcommand_uses_existing_dev_mode(self) -> None:
        with patch("core.web.app._dev", return_value=0) as dev:
            result = web_app.main(["dev", "--skip-install"])

        self.assertEqual(0, result)
        dev.assert_called_once_with(["--skip-install"])

    def test_missing_dist_reports_build_and_dev_instructions(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(web_app, "DIST_INDEX", Path(tmp) / "dist" / "index.html"),
            redirect_stderr(StringIO()) as stderr,
        ):
            result = web_app.main(["--no-open"])

        self.assertEqual(1, result)
        output = stderr.getvalue()
        self.assertIn("cd frontend && npm run build", output)
        self.assertIn("python main.py dev", output)

    def test_no_open_starts_production_without_browser_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontend_dir = Path(tmp) / "frontend"
            dist_dir = frontend_dir / "dist"
            src_dir = frontend_dir / "src"
            dist_dir.mkdir(parents=True)
            src_dir.mkdir()
            index = dist_dir / "index.html"
            index.write_text("<main>TraceLog</main>", encoding="utf-8")
            production_app = object()

            with (
                patch.object(web_app, "DIST_DIR", dist_dir),
                patch.object(web_app, "DIST_INDEX", index),
                patch.object(web_app, "FRONTEND_SRC_DIR", src_dir),
                patch("core.web.app._find_available_port", return_value=8123),
                patch("core.web.app.create_production_app", return_value=production_app),
                patch("core.web.app.uvicorn.run") as run,
                patch("core.web.app.threading.Thread") as thread,
                redirect_stdout(StringIO()),
            ):
                result = web_app.main(["serve", "--port", "8123", "--no-open"])

        self.assertEqual(0, result)
        thread.assert_not_called()
        run.assert_called_once_with(production_app, host="127.0.0.1", port=8123)

    def test_zero_port_prints_shell_protocol_before_starting_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist_dir = Path(tmp) / "dist"
            dist_dir.mkdir()
            index = dist_dir / "index.html"
            index.write_text("<main>TraceLog</main>", encoding="utf-8")

            with (
                patch.object(web_app, "DIST_DIR", dist_dir),
                patch.object(web_app, "DIST_INDEX", index),
                patch.object(web_app, "FRONTEND_SRC_DIR", Path(tmp) / "src"),
                patch("core.web.app._find_available_port", return_value=43123),
                patch("core.web.app.create_production_app", return_value=object()),
                patch("core.web.app.uvicorn.run"),
                redirect_stdout(StringIO()) as stdout,
            ):
                result = web_app.main(["serve", "--port", "0", "--no-open"])

        self.assertEqual(0, result)
        self.assertIn("TRACELOG_PORT=43123\n", stdout.getvalue())

    def test_browser_opens_after_health_check_succeeds(self) -> None:
        stop_event = web_app.threading.Event()
        with (
            patch("core.web.app.urllib.request.urlopen") as urlopen,
            patch("core.web.app.webbrowser.open") as open_browser,
        ):
            urlopen.return_value.__enter__.return_value.status = 200

            web_app._open_browser_when_ready("http://127.0.0.1:8123", stop_event)

        urlopen.assert_called_once_with("http://127.0.0.1:8123/api/health", timeout=1)
        open_browser.assert_called_once_with("http://127.0.0.1:8123/")

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
        self.assertIn("--strictPort", command)

    def test_find_available_port_skips_occupied_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            occupied_port = sock.getsockname()[1]

            port = web_app._find_available_port("127.0.0.1", occupied_port)

        self.assertNotEqual(occupied_port, port)

    def test_find_available_port_zero_returns_ephemeral_port(self) -> None:
        socket_context = Mock()
        socket_context.__enter__ = Mock(return_value=socket_context)
        socket_context.__exit__ = Mock(return_value=False)
        socket_context.getsockname.return_value = ("127.0.0.1", 43123)
        with patch("core.web.app.socket.socket", return_value=socket_context):
            port = web_app._find_available_port("127.0.0.1", 0)

        self.assertEqual(43123, port)
        socket_context.bind.assert_called_once_with(("127.0.0.1", 0))

    def test_assign_ports_keeps_backend_and_frontend_distinct(self) -> None:
        backend_port = _free_port()
        args = argparse.Namespace(
            host="127.0.0.1",
            backend_port=backend_port,
            frontend_port=backend_port,
        )

        web_app._assign_ports(args)

        self.assertEqual(backend_port, args.backend_port)
        self.assertNotEqual(args.backend_port, args.frontend_port)

    def test_backend_url_uses_loopback_for_wildcard_host(self) -> None:
        args = argparse.Namespace(host="0.0.0.0", backend_port=8000)

        self.assertEqual("http://127.0.0.1:8000", web_app._backend_url(args))

    def test_start_merges_extra_environment(self) -> None:
        with patch("core.web.app.subprocess.Popen") as popen:
            web_app._start(
                ["server"],
                cwd=web_app.ROOT,
                name="server",
                env={"TRACELOG_BACKEND_URL": "http://127.0.0.1:8010"},
            )

        kwargs = popen.call_args.kwargs
        self.assertEqual("http://127.0.0.1:8010", kwargs["env"]["TRACELOG_BACKEND_URL"])

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
