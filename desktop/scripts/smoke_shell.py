"""Smoke-test the packaged Electron shell and its frozen engine."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path


def _remove_temporary_data_dir(data_dir: Path) -> None:
    resolved = data_dir.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        resolved.parent != temporary_root
        or not resolved.name.startswith("tracelog-shell-smoke-")
    ):
        raise RuntimeError(
            f"Refusing to remove unexpected smoke directory: {resolved}"
        )

    last_error: PermissionError | None = None
    for _ in range(20):
        try:
            shutil.rmtree(resolved)
            return
        except FileNotFoundError:
            return
        except PermissionError as error:
            last_error = error
            time.sleep(0.25)
    if last_error is not None:
        raise last_error


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def smoke(shell_executable: Path) -> None:
    if not shell_executable.is_file():
        raise FileNotFoundError(f"Packaged Electron shell not found: {shell_executable}")

    temporary_root = Path(tempfile.mkdtemp(prefix="tracelog-shell-smoke-"))
    engine_data_dir = temporary_root / "data"
    smoke_marker = temporary_root / "shell-loaded"
    environment = os.environ.copy()
    environment["TRACELOG_DATA_DIR"] = str(engine_data_dir)
    environment["TRACELOG_DESKTOP_SMOKE"] = "1"
    environment["TRACELOG_DESKTOP_SMOKE_MARKER"] = str(smoke_marker)
    # Electron derives userData from the per-user config root: %APPDATA% on
    # Windows, $HOME/Library/Application Support on macOS. Redirect whichever
    # one applies so the packaged shell never touches real user data.
    if os.name == "nt":
        environment["APPDATA"] = str(temporary_root / "appdata")
    else:
        electron_home = temporary_root / "home"
        electron_home.mkdir(parents=True, exist_ok=True)
        environment["HOME"] = str(electron_home)
    popen_options: dict[str, object] = {
        "cwd": temporary_root,
        "env": environment,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NO_WINDOW

    process = subprocess.Popen([str(shell_executable)], **popen_options)
    output = ""
    try:
        try:
            output, _ = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            _stop_process_tree(process)
            raise TimeoutError("Packaged Electron shell smoke test timed out")

        if process.returncode != 0:
            raise RuntimeError(
                f"Packaged Electron shell exited with code {process.returncode}\n{output}"
            )
        if (
            not smoke_marker.is_file()
            or smoke_marker.read_text(encoding="utf-8").strip()
            != "TRACELOG_DESKTOP_SMOKE_OK"
        ):
            raise AssertionError(
                f"Packaged Electron shell did not finish loading\n{output}"
            )

        database = engine_data_dir / "workspace" / "state.db"
        if not database.is_file():
            raise AssertionError("Packaged Electron shell did not initialize state.db")
        connection = sqlite3.connect(database)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
        if result != ("ok",):
            raise AssertionError(f"Packaged state.db quick_check failed: {result!r}")
    finally:
        _stop_process_tree(process)
        _remove_temporary_data_dir(temporary_root)

    print("Packaged Electron shell smoke test passed.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("shell_executable", type=Path)
    args = parser.parse_args()
    smoke(args.shell_executable.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
