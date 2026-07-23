"""PyInstaller entrypoint for the TraceLog desktop engine."""

from __future__ import annotations

import os
import signal
import sys
import threading

from core.web.app import main


def _stop_when_parent_pipe_closes() -> None:
    sys.stdin.buffer.read()
    if os.name == "nt":
        signal.raise_signal(signal.SIGINT)
    else:
        os.kill(os.getpid(), signal.SIGTERM)


if __name__ == "__main__":
    if os.environ.get("TRACELOG_PARENT_PIPE") == "1":
        threading.Thread(target=_stop_when_parent_pipe_closes, daemon=True).start()
    sys.exit(main(["serve", "--host", "127.0.0.1", "--port", "0", "--no-open"]))
