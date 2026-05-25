"""Compatibility facade for LLM router functions.

New code should import the domain router it needs from core.llm.*.
"""

from __future__ import annotations

from core.llm.reflection_router import (
    call_global_deep_reflection,
    call_light_reflection,
    call_soul_deep_reflection,
)
from core.llm.reply_router import (
    call_post_reply,
    call_soul_chat_reply,
    call_soul_comment_reply,
    call_soul_post_reply,
)
from core.llm.todo_router import call_todo_tool

__all__ = [
    "call_global_deep_reflection",
    "call_light_reflection",
    "call_post_reply",
    "call_soul_chat_reply",
    "call_soul_comment_reply",
    "call_soul_deep_reflection",
    "call_soul_post_reply",
    "call_todo_tool",
]
