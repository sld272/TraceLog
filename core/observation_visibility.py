"""Central visibility policy for Observation memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


GLOBAL = "global"
SOUL_SCOPED = "soul_scoped"
PRIVATE_BLOCKED = "private_blocked"

EVIDENCE_ALL = "all"
EVIDENCE_SOURCE_SOUL_ONLY = "source_soul_only"
EVIDENCE_NONE = "none"


@dataclass(frozen=True)
class ObservationBoundary:
    visibility_scope: str
    evidence_access: str
    scope_post_id: str | None = None
    scope_soul_name: str | None = None


class _MemoryHit(Protocol):
    @property
    def scope_soul_name(self) -> str | None:
        ...


class _RetrievalScope(Protocol):
    @property
    def channel(self) -> str:
        ...

    @property
    def soul_name(self) -> str | None:
        ...


def boundary_for_public_post() -> ObservationBoundary:
    return ObservationBoundary(
        visibility_scope=GLOBAL,
        evidence_access=EVIDENCE_ALL,
    )


def boundary_for_chat_thread(soul_name: str) -> ObservationBoundary:
    return ObservationBoundary(
        visibility_scope=SOUL_SCOPED,
        scope_soul_name=soul_name,
        evidence_access=EVIDENCE_SOURCE_SOUL_ONLY,
    )


def boundary_for_comment_thread(soul_name: str, post_id: str) -> ObservationBoundary:
    return ObservationBoundary(
        visibility_scope=SOUL_SCOPED,
        scope_soul_name=soul_name,
        scope_post_id=post_id,
        evidence_access=EVIDENCE_SOURCE_SOUL_ONLY,
    )


def allowed_memory_scopes(channel: str, soul_name: str | None = None) -> list[tuple[str, str | None]]:
    scopes: list[tuple[str, str | None]] = [(GLOBAL, None)]
    if channel in {"public_post_soul", "chat", "comment_thread"} and soul_name:
        scopes.append((SOUL_SCOPED, soul_name))
    return scopes


def can_expand_evidence(hit: _MemoryHit, evidence_access: str, retrieval_scope: _RetrievalScope) -> bool:
    if evidence_access == EVIDENCE_ALL:
        return True
    if evidence_access == EVIDENCE_SOURCE_SOUL_ONLY:
        return (
            retrieval_scope.channel in {"chat", "comment_thread"}
            and hit.scope_soul_name is not None
            and hit.scope_soul_name == retrieval_scope.soul_name
        )
    return False
