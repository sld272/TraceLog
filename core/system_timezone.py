"""System-local timezone values shared by schedule and Graph integrations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

SYSTEM_TIMEZONE = datetime.now().astimezone().tzinfo


def _system_timezone_name() -> str:
    key = getattr(SYSTEM_TIMEZONE, "key", None)
    if isinstance(key, str) and key:
        return key
    localtime = Path("/etc/localtime")
    if localtime.exists():
        resolved_parts = localtime.resolve().parts
        if "zoneinfo" in resolved_parts:
            marker = resolved_parts.index("zoneinfo")
            return "/".join(resolved_parts[marker + 1 :])
    # Windows exposes names such as "China Standard Time" through tzinfo
    # instead of an /etc/localtime zoneinfo symlink.
    return str(SYSTEM_TIMEZONE)


SYSTEM_TIMEZONE_NAME = _system_timezone_name()
