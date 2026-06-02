"""Start the TraceLog API and Vite frontend together."""

from __future__ import annotations

import argparse
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT / "frontend"
VITE_ENTRYPOINT = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start TraceLog Web development servers.")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--skip-install", action="store_true")
    args = parser.parse_args(argv)

    npm = _require_command("npm")
    node = _require_command("node")
    if not args.skip_install:
        _ensure_frontend_dependencies(npm)
    _assign_ports(args)

    processes: list[subprocess.Popen] = []
    try:
        backend = _start(
            _backend_command(args),
            cwd=ROOT,
            name="api",
        )
        processes.append(backend)

        frontend = _start(
            _frontend_command(args, node),
            cwd=FRONTEND_DIR,
            name="frontend",
            env={"TRACELOG_BACKEND_URL": _backend_url(args)},
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
        _stop_all(processes)


def _ensure_frontend_dependencies(npm: str) -> None:
    if (FRONTEND_DIR / "node_modules").exists():
        return
    print("Installing frontend dependencies...", flush=True)
    subprocess.run([npm, "install"], cwd=FRONTEND_DIR, check=True)


def _assign_ports(args: argparse.Namespace) -> None:
    requested_backend_port = args.backend_port
    requested_frontend_port = args.frontend_port
    used_ports: set[int] = set()
    args.backend_port = _find_available_port(args.host, requested_backend_port, used_ports)
    used_ports.add(args.backend_port)
    args.frontend_port = _find_available_port(args.host, requested_frontend_port, used_ports)

    if args.backend_port != requested_backend_port:
        print(
            f"TraceLog API port {requested_backend_port} is in use; using {args.backend_port}.",
            flush=True,
        )
    if args.frontend_port != requested_frontend_port:
        print(
            f"TraceLog Web port {requested_frontend_port} is in use; using {args.frontend_port}.",
            flush=True,
        )


def _find_available_port(host: str, preferred_port: int, used_ports: set[int] | None = None) -> int:
    used_ports = used_ports or set()
    port = preferred_port
    while port < 65536:
        if port not in used_ports and _can_bind(host, port):
            return port
        port += 1
    raise SystemExit(f"No available port found starting at {preferred_port}")


def _can_bind(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _backend_url(args: argparse.Namespace) -> str:
    host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    return f"http://{host}:{args.backend_port}"


def _backend_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "api.app:app",
        "--host",
        args.host,
        "--port",
        str(args.backend_port),
    ]


def _frontend_command(args: argparse.Namespace, node: str) -> list[str]:
    return [
        node,
        str(VITE_ENTRYPOINT),
        "--host",
        args.host,
        "--port",
        str(args.frontend_port),
        "--strictPort",
    ]


def _start(
    command: list[str],
    *,
    cwd: Path,
    name: str,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    print(f"Starting {name}: {' '.join(command)}", flush=True)
    kwargs: dict = {"cwd": cwd}
    if env is not None:
        process_env = os.environ.copy()
        process_env.update(env)
        kwargs["env"] = process_env
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


def _stop_all(processes: list[subprocess.Popen]) -> None:
    interrupted = False
    for process in reversed(processes):
        try:
            _stop(process)
        except KeyboardInterrupt:
            interrupted = True
            _kill(process)
    if interrupted:
        print("Forced TraceLog Web shutdown.", flush=True)
    for process in reversed(processes):
        _kill(process)


def _stop_posix_process_group(process: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    for sig, timeout in (
        (signal.SIGINT, 8),
        (signal.SIGTERM, 3),
        (getattr(signal, "SIGKILL", signal.SIGTERM), 1),
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
    except KeyboardInterrupt:
        raise
    except Exception:
        _kill_windows_process_tree(process)


def _stop_plain_process(process: subprocess.Popen) -> None:
    try:
        process.terminate()
        process.wait(timeout=8)
    except Exception:
        process.kill()


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        _kill_windows_process_tree(process)
        return
    try:
        process.kill()
    except Exception:
        return
    try:
        process.wait(timeout=3)
    except Exception:
        pass


def _kill_windows_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    try:
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _require_command(command: str) -> str:
    resolved = shutil.which(command)
    if resolved is None:
        raise SystemExit(f"Missing required command: {command}")
    return resolved
