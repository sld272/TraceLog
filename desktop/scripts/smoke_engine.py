"""Smoke-test a frozen TraceLog engine on macOS or Windows."""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TextIO


PORT_PATTERN = re.compile(r"^TRACELOG_PORT=(\d+)$")
START_TIMEOUT_SECONDS = 30


def _read_output(
    stream: TextIO,
    lines: queue.Queue[str],
    log_path: Path,
) -> None:
    with log_path.open("w", encoding="utf-8") as log:
        for line in stream:
            log.write(line)
            log.flush()
            lines.put(line)


def _wait_for_port(
    process: subprocess.Popen[str],
    lines: queue.Queue[str],
) -> int:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Frozen engine exited during startup with code {process.returncode}"
            )
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        match = PORT_PATTERN.fullmatch(line.strip())
        if match:
            return int(match.group(1))
    raise TimeoutError("Timed out waiting for TRACELOG_PORT")


def _request(url: str, timeout: float = 5) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def _wait_for_health(base_url: str) -> None:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    health_url = f"{base_url}/api/health"
    while time.monotonic() < deadline:
        try:
            _request(health_url, timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"Timed out waiting for {health_url}")


def _seed_search_record(data_dir: Path) -> None:
    database = data_dir / "workspace" / "state.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            INSERT INTO posts(
                id, ts, content, importance, created_at, updated_at
            ) VALUES (
                'desktop-smoke',
                '2026-07-23T12:00:00+08:00',
                '冻结检索冒烟',
                0.5,
                1.0,
                1.0
            );
            INSERT INTO post_events(
                post_id, job_id, event_type, payload_json, created_at
            ) VALUES (
                'desktop-smoke',
                NULL,
                'pipeline_done',
                '{"smoke":true}',
                1.0
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _assert_search(base_url: str, mode: str | None = None) -> None:
    parameters = {"q": "冻结检索"}
    if mode is not None:
        parameters["mode"] = mode
    payload = _request(
        f"{base_url}/api/posts/search?{urllib.parse.urlencode(parameters)}"
    )
    if b"desktop-smoke" not in payload:
        raise AssertionError(f"Frozen search failed for mode={mode!r}")


def _assert_event_stream(base_url: str) -> None:
    url = f"{base_url}/api/posts/desktop-smoke/events?after_id=0"
    with urllib.request.urlopen(url, timeout=5) as response:
        while True:
            line = response.readline()
            if not line:
                break
            if b"event: pipeline_done" in line:
                return
    raise AssertionError("Frozen event stream did not return pipeline_done")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=8)
        return
    except subprocess.TimeoutExpired:
        pass

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _remove_temporary_data_dir(data_dir: Path) -> None:
    resolved = data_dir.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        resolved.parent != temporary_root
        or not resolved.name.startswith("tracelog-smoke-")
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


def smoke(engine: Path) -> None:
    if not engine.is_file():
        raise FileNotFoundError(f"Frozen engine not found: {engine}")

    data_dir = Path(tempfile.mkdtemp(prefix="tracelog-smoke-"))
    log_path = data_dir / "engine.log"
    environment = os.environ.copy()
    environment["TRACELOG_DATA_DIR"] = str(data_dir)
    environment["TRACELOG_PARENT_PIPE"] = "1"
    popen_options: dict[str, object] = {
        "cwd": data_dir,
        "env": environment,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        popen_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_options["start_new_session"] = True

    process = subprocess.Popen([str(engine)], **popen_options)
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=_read_output,
        args=(process.stdout, lines, log_path),
        daemon=True,
    )
    reader.start()
    try:
        port = _wait_for_port(process, lines)
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(base_url)
        if b"<!doctype html>" not in _request(f"{base_url}/").lower():
            raise AssertionError("Frozen engine did not serve the frontend")
        _seed_search_record(data_dir)
        _assert_search(base_url)
        _assert_search(base_url, "hybrid")
        _assert_event_stream(base_url)
    except Exception:
        if log_path.is_file():
            log = log_path.read_text(encoding="utf-8", errors="replace")
            output_encoding = sys.stdout.encoding or "utf-8"
            print(
                log.encode(
                    output_encoding,
                    errors="backslashreplace",
                ).decode(output_encoding)
            )
        raise
    finally:
        _stop_process(process)
        reader.join(timeout=2)
        _remove_temporary_data_dir(data_dir)

    print(f"Frozen engine smoke test passed on port {port}.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", type=Path)
    args = parser.parse_args()
    smoke(args.engine.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
