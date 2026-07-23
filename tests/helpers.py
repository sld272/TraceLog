from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TypeVar

T = TypeVar("T")


def require_not_none(value: T | None) -> T:
    assert value is not None
    return value


def stream_delta_chunk(text: str) -> SimpleNamespace:
    """A streamed chunk carrying one text delta."""
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class FakeStreamingClient:
    """Fake OpenAI-compatible client for streaming chat replies.

    ``create(stream=True)`` returns a chunk iterator that leads with an
    empty-``choices`` keep-alive chunk (to exercise that tolerance), yields one
    chunk per delta, and — when ``raise_after`` is set — raises mid-stream after
    that many deltas. ``create(stream=False)`` returns a normal completion
    carrying ``non_stream_reply`` (the non-streaming fallback path)."""

    def __init__(
        self,
        deltas: list[str] | None = None,
        *,
        non_stream_reply: str = "非流式降级回复",
        raise_after: int | None = None,
    ) -> None:
        self.deltas = ["你好", "，", "在的"] if deltas is None else deltas
        self.non_stream_reply = non_stream_reply
        self.raise_after = raise_after
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    @property
    def stream_calls(self) -> list[dict]:
        return [call for call in self.calls if call.get("stream")]

    @property
    def non_stream_calls(self) -> list[dict]:
        return [call for call in self.calls if not call.get("stream")]

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return self._chunk_iter()
        content = json.dumps({"reply": self.non_stream_reply}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    def _chunk_iter(self):
        yield SimpleNamespace(choices=[])  # empty-choices chunk: must be tolerated
        emitted = 0
        for delta in self.deltas:
            yield stream_delta_chunk(delta)
            emitted += 1
            if self.raise_after is not None and emitted >= self.raise_after:
                raise RuntimeError("stream broke")
