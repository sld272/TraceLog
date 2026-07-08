"""Read-side boundary policy: which memory a SOUL may retrieve in each scene.

This encodes the product's desired human-like boundary model (orthogonal to the
write-side owner/visibility labels):

  * Comments under a post are a PUBLIC scene: every SOUL may retrieve the user's
    public conversations with *any* SOUL (posts + all comment threads) and
    reference them.
  * Private-chat memory is retrievable only by the SOUL it belongs to.
  * When replying in a public scene, a SOUL may still retrieve its OWN private
    memory, but must judge whether it is appropriate to reveal publicly (a soft
    discretion gate, like a real person — NOT a hard wall).

The read path uses this module to filter and annotate every candidate before it
can reach a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

# reply scenes
PUBLIC_CHANNELS = frozenset({"public_post", "comment"})
PRIVATE_CHANNELS = frozenset({"chat"})

# a unit is admissible either freely ("hard") or with discretion ("soft");
# otherwise it is "forbidden" and must never reach the prompt.
HARD = "hard"
SOFT = "soft"
FORBIDDEN = "forbidden"


def is_public_visibility(visibility_scope: str) -> bool:
    """Posts and comment threads are all part of the public scene."""
    return visibility_scope == "public" or visibility_scope.startswith("thread:")


def private_soul_of(visibility_scope: str) -> str | None:
    prefix = "private:soul:"
    if visibility_scope.startswith(prefix):
        return visibility_scope[len(prefix):]
    return None


@dataclass(frozen=True)
class ScopeDecision:
    verdict: str  # HARD | SOFT | FORBIDDEN

    @property
    def admissible(self) -> bool:
        return self.verdict != FORBIDDEN

    @property
    def needs_discretion(self) -> bool:
        return self.verdict == SOFT


def classify(visibility_scope: str, *, channel: str, reply_soul: str | None) -> ScopeDecision:
    """Decide how a unit with ``visibility_scope`` may be used when ``reply_soul``
    replies in ``channel``.

    ``reply_soul`` is None for scenes with no acting SOUL (e.g. pure user-facing
    tools); such callers only ever get public memory.
    """
    if is_public_visibility(visibility_scope):
        return ScopeDecision(HARD)

    owner_soul = private_soul_of(visibility_scope)
    if owner_soul is None:
        return ScopeDecision(FORBIDDEN)

    # private memory: only the owning SOUL may ever touch it
    if reply_soul is None or owner_soul != reply_soul:
        return ScopeDecision(FORBIDDEN)

    # own private memory: free in private scenes, discretion-gated in public ones
    if channel in PUBLIC_CHANNELS:
        return ScopeDecision(SOFT)
    return ScopeDecision(HARD)


def admissible_visibility_filters(channel: str, reply_soul: str | None) -> dict:
    """Describe, for a retrieval query, which visibility scopes to admit.

    Returns a plan the retrieval layer can translate into SQL / ANN metadata
    filters without re-deriving policy:
      - public: include all public-scene units (visibility 'public' or 'thread:%')
      - private_self: the reply soul's own private scope, or None
      - private_self_needs_discretion: whether that private memory must be
        flagged for the model to self-censor before public disclosure
    """
    plan = {
        "public": True,
        "private_self": None,
        "private_self_needs_discretion": False,
    }
    if reply_soul is not None:
        decision = classify(f"private:soul:{reply_soul}", channel=channel, reply_soul=reply_soul)
        if decision.admissible:
            plan["private_self"] = f"private:soul:{reply_soul}"
            plan["private_self_needs_discretion"] = decision.needs_discretion
    return plan
