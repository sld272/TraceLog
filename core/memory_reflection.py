"""Owner-level deep reflection (P2): the slow consolidation clock.

The fast per-(owner, visibility) reconcile keeps beliefs current incrementally;
this pass runs low-frequency over a WHOLE persona (one ``owner_scope``, spanning
its public + private visibility layers) and does the holistic work the
event-driven reconcile structurally cannot:

  * decay  — a stale, non-core, reflected belief that has not been re-confirmed
    within the window drops to ``dormant``. Dormant units leave every read path
    AND the reconcile comparison set (which loads only active/challenged), so the
    persona's prompt stops growing without bound. This is the deterministic fix
    for "memory only ever grows". Reversible: a future confirm revives it.
  * promote — a contextual belief that has been re-confirmed across enough
    reflections (and is important + confident) sediments into ``core`` and may
    enter the always-on portrait. Time-sedimented entry replaces the single-pass
    tier gamble that left obviously-core beliefs stuck at contextual.

Both steps are fully deterministic (no LLM), so this module is unit-testable on
its own. The LLM-driven consolidation (dedup / contradiction / cross-layer merge)
plugs in as a separate injectable seam (P2b) and is intentionally NOT here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core import db, logging_service, memory_unit_service as mus, memory_view_service as mvs

# A reflected contextual/episodic belief not re-confirmed within this window is
# considered stale and retired to dormant.
DECAY_WINDOW_DAYS = 30.0
DAY_SECONDS = 86400.0

# A contextual belief needs this many confirms (re-evidencing across later
# interactions) before reflection sediments it into core — "survived N passes".
PROMOTE_MIN_CONFIRMS = 2


@dataclass
class ReflectionSummary:
    owner_scope: str
    decayed: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.decayed) + len(self.promoted)


def decay_dormant(
    owner_scope: str,
    *,
    now: float | None = None,
    window_days: float = DECAY_WINDOW_DAYS,
    conn,
) -> list[str]:
    """Retire stale, non-core, reflected beliefs in this owner to dormant.

    Skips core (the identity floor), user-authored beliefs, challenged units, and
    anything the user pinned into the portrait (force_include)."""
    now = db.now_ts() if now is None else now
    cutoff = now - window_days * DAY_SECONDS
    rows = conn.execute(
        """
        SELECT id FROM memory_units
        WHERE owner_scope = ?
          AND status = 'active'
          AND source = 'reflected'
          AND tier IN ('contextual','episodic')
          AND portrait_policy != 'force_include'
          AND last_confirmed < ?
        ORDER BY id
        """,
        (owner_scope, cutoff),
    ).fetchall()
    decayed: list[str] = []
    for row in rows:
        mus.decay_unit(str(row["id"]), conn=conn)
        decayed.append(str(row["id"]))
    return decayed


def promote_core(owner_scope: str, *, conn) -> list[str]:
    """Sediment contextual beliefs that are important, confident, and re-confirmed
    enough times into core. Respects a user force_exclude."""
    rows = conn.execute(
        """
        SELECT id FROM memory_units
        WHERE owner_scope = ?
          AND status = 'active'
          AND source = 'reflected'
          AND tier = 'contextual'
          AND portrait_policy != 'force_exclude'
          AND confidence >= ?
          AND importance >= ?
        ORDER BY id
        """,
        (owner_scope, mvs.ENTER, mvs.MIN_IMPORTANCE),
    ).fetchall()
    promoted: list[str] = []
    for row in rows:
        unit_id = str(row["id"])
        if mus.count_confirm_ops(unit_id, conn=conn) >= PROMOTE_MIN_CONFIRMS:
            mus.promote_unit_tier(unit_id, tier="core", conn=conn)
            promoted.append(unit_id)
    return promoted


def reflect_persona(owner_scope: str, *, now: float | None = None) -> ReflectionSummary:
    """Run one deterministic deep-reflection pass over a whole persona/owner.

    Decay then promote, in one transaction. View staleness is propagated by the
    underlying mus primitives, so the background view refresh re-synthesizes the
    persona's portrait / relationship memory afterward."""
    mus.validate_owner_scope(owner_scope)
    summary = ReflectionSummary(owner_scope=owner_scope)
    with db.immediate_transaction() as conn:
        summary.decayed = decay_dormant(owner_scope, now=now, conn=conn)
        summary.promoted = promote_core(owner_scope, conn=conn)
    logging_service.log_event(
        "memory_reflection",
        owner_scope=owner_scope,
        decayed=len(summary.decayed),
        promoted=len(summary.promoted),
    )
    return summary


def reflect_all_personas(*, now: float | None = None) -> list[ReflectionSummary]:
    """Reflect every owner that currently has any memory units."""
    owners = [
        str(row["owner_scope"])
        for row in db.query_all(
            "SELECT DISTINCT owner_scope FROM memory_units ORDER BY owner_scope"
        )
    ]
    return [reflect_persona(owner, now=now) for owner in owners]
