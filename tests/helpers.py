from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def require_not_none(value: T | None) -> T:
    assert value is not None
    return value
