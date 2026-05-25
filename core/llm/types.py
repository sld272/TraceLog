"""Shared typing helpers for LLM clients."""

from __future__ import annotations

from typing import Any, Protocol


class LLMClient(Protocol):
    """Minimal client surface used by TraceLog LLM routers."""

    chat: Any
