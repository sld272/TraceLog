"""Small cross-platform seam for private application data."""

from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path


def make_private(path: str | os.PathLike[str]) -> None:
    """Restrict a path to the current account and OS administrators."""
    target = Path(path)
    if _is_windows():
        _make_private_windows(target)
        return
    target.chmod(0o700 if target.is_dir() else 0o600)


def _is_windows() -> bool:
    return os.name == "nt"


def _make_private_windows(path: Path) -> None:
    principal = _windows_principal()
    access = "(OI)(CI)F" if path.is_dir() else "F"
    command = [
        "icacls.exe",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{principal}:{access}",
        f"*S-1-5-18:{access}",
        f"*S-1-5-32-544:{access}",
        "/Q",
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.strip()
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else ""
        )
        message = f"无法收紧路径权限：{path}"
        if detail:
            message = f"{message}（{detail}）"
        raise OSError(message) from exc


def _windows_principal() -> str:
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    if domain and "\\" not in username:
        return f"{domain}\\{username}"
    return username
