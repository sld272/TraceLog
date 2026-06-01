"""Start the TraceLog API and Vite frontend together."""

from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"


def main() -> int:
    parser = argparse.ArgumentParser(description="Start TraceLog Web development servers.")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--skip-install", action="store_true")
    args = parser.parse_args()

    _require_command("npm")
    if not args.skip_install:
        _ensure_frontend_dependencies()

    processes: list[subprocess.Popen] = []
    try:
        backend = _start(
            _backend_command(args),
            cwd=ROOT,
            name="api",
        )
        processes.append(backend)

        frontend = _start(
            _frontend_command(args),
            cwd=FRONTEND_DIR,
            name="frontend",
        )
        processes.append(frontend)

        print("", flush=True)
        print(f"TraceLog Web: http://{args.host}:{args.frontend_port}/", flush=True)
        print(f"TraceLog API: http://{args.host}:{args.backend_port}/health", flush=True)
        print("Press Ctrl+C to stop both servers.", flush=True)

        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    print(f"\nA server exited with code {code}; stopping the rest.", flush=True)
                    return code
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping TraceLog Web...", flush=True)
        return 0
    finally:
        for process in reversed(processes):
            _stop(process)


def _ensure_frontend_dependencies() -> None:
    if (FRONTEND_DIR / "node_modules").exists():
        return
    print("Installing frontend dependencies...", flush=True)
    subprocess.run(["npm", "install"], cwd=FRONTEND_DIR, check=True)


def _backend_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "api.app:app",
        "--reload",
        "--host",
        args.host,
        "--port",
        str(args.backend_port),
    ]


def _frontend_command(args: argparse.Namespace) -> list[str]:
    return [
        "npm",
        "run",
        "dev",
        "--",
        "--host",
        args.host,
        "--port",
        str(args.frontend_port),
    ]


def _start(command: list[str], *, cwd: Path, name: str) -> subprocess.Popen:
    print(f"Starting {name}: {' '.join(command)}", flush=True)
    kwargs: dict = {"cwd": cwd}
    if os.name == "posix":
        kwargs["preexec_fn"] = os.setsid
    elif os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(command, **kwargs)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        _stop_posix_process_group(process)
        return
    if os.name == "nt":
        _stop_windows_process(process)
        return
    _stop_plain_process(process)


def _stop_posix_process_group(process: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    for sig, timeout in (
        (signal.SIGINT, 8),
        (signal.SIGTERM, 3),
        (signal.SIGKILL, 1),
    ):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            continue


def _stop_windows_process(process: subprocess.Popen) -> None:
    try:
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
        process.send_signal(ctrl_break)
        process.wait(timeout=8)
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=3)
        except Exception:
            process.kill()


def _stop_plain_process(process: subprocess.Popen) -> None:
    try:
        process.terminate()
        process.wait(timeout=8)
    except Exception:
        process.kill()


def _require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise SystemExit(f"Missing required command: {command}")


if __name__ == "__main__":
    sys.exit(main())
