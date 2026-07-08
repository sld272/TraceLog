"""Owner-level deep reflection (P2): the slow consolidation clock.

The fast per-(owner, visibility) reconcile keeps beliefs current incrementally;
this pass runs low-frequency over a WHOLE persona (one ``owner_scope``, spanning
its public + private visibility layers) and does the holistic work the
event-driven reconcile structurally cannot:

  * decay  — a stale, non-core, reflected ``state`` that has not been re-confirmed
    within the window drops to ``dormant``. Only ``state`` ages out: it is a
    transient "current status", so one nobody has reconfirmed in a month is no
    longer current. Durable beliefs (preference/identity/insight/…) never decay by
    age — they persist and are recalled on relevance, leaving only via promotion to
    core or explicit retraction. Dormant units leave every read path AND the
    reconcile comparison set (which loads only active/challenged), so the persona's
    prompt stops growing without bound. This is the deterministic fix for "state
    memory only ever grows". Reversible: a future confirm revives it.
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
    """Retire stale, non-core, reflected ``state`` units in this owner to dormant.

    Only ``type='state'`` decays by age — a transient current-status nobody has
    reconfirmed within the window is no longer current. Durable beliefs persist
    indefinitely (recalled on relevance) and are never aged out here. Skips core
    (the identity floor), user-authored beliefs, challenged units, and anything the
    user pinned into the portrait (force_include)."""
    now = db.now_ts() if now is None else now
    cutoff = now - window_days * DAY_SECONDS
    rows = conn.execute(
        """
        SELECT id FROM memory_units
        WHERE owner_scope = ?
          AND type = 'state'
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


@dataclass
class ConsolidationSummary:
    owner_scope: str
    merged: list[str] = field(default_factory=list)     # absorbed unit ids superseded
    retracted: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.merged) + len(self.retracted)


def _active_status(conn, unit_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM memory_units WHERE id = ?", (unit_id,)
    ).fetchone()
    return row is not None and row["status"] == "active"


def consolidate_persona(owner_scope: str, *, producer) -> ConsolidationSummary:
    """LLM-driven consolidation over one owner's active units: merge duplicates,
    retract contradictions. ``producer(owner_scope, units) -> {ops, summary}`` is
    injected (the LLM seam), so this stays deterministic + unit-testable.

    Every op is validated against the live state inside the transaction: targets
    must still be active and belong to this owner; the iron law (a survivor must
    be at least as private as what it absorbs) is enforced by supersede_unit.
    Per-op failures are skipped + audited; they never abort the batch."""
    mus.validate_owner_scope(owner_scope)
    units = mus.list_active_units_for_owner(owner_scope)
    summary = ConsolidationSummary(owner_scope=owner_scope)
    if not units:
        return summary
    units_by_id = {str(u["id"]): u for u in units}
    result = producer(owner_scope=owner_scope, units=[dict(u) for u in units]) or {}
    ops = result.get("ops") if isinstance(result, dict) else None
    if not isinstance(ops, list):
        ops = []

    with db.immediate_transaction() as conn:
        for op in ops:
            try:
                kind = str(op.get("op"))
                if kind == "merge":
                    survivor = str(op.get("survivor_id") or "")
                    absorbed = [str(x) for x in (op.get("absorbed_ids") or [])]
                    if survivor not in units_by_id or not _active_status(conn, survivor):
                        raise ValueError(f"merge survivor 不是本主体的 active unit：{survivor}")
                    targets = []
                    for absorbed_id in absorbed:
                        if absorbed_id == survivor:
                            raise ValueError("merge survivor 不能并入自身")
                        if absorbed_id not in units_by_id or not _active_status(conn, absorbed_id):
                            raise ValueError(f"merge absorbed 不是本主体的 active unit：{absorbed_id}")
                        targets.append(absorbed_id)
                    # iron law, pre-checked for the whole op so a violating merge
                    # applies nothing (the survivor must be at least as private as
                    # every unit it absorbs).
                    survivor_rank = mus.visibility_rank(units_by_id[survivor]["visibility_scope"])
                    for absorbed_id in targets:
                        if mus.visibility_rank(units_by_id[absorbed_id]["visibility_scope"]) > survivor_rank:
                            raise ValueError(
                                f"merge 违反铁律：survivor 比 {absorbed_id} 更公开，不能把更私密的信念并入"
                            )
                    content = op.get("content")
                    # only rewrite a reflected survivor's wording; never overwrite
                    # the user's own words with a model-merged paraphrase
                    if (
                        isinstance(content, str)
                        and content.strip()
                        and units_by_id[survivor]["source"] == "reflected"
                    ):
                        mus.revise_unit(survivor, content=content.strip(), actor="consolidation", conn=conn)
                    for absorbed_id in targets:
                        mus.supersede_unit(absorbed_id, survivor, actor="consolidation", conn=conn)
                        summary.merged.append(absorbed_id)
                elif kind == "retract":
                    target = str(op.get("target_id") or "")
                    if target not in units_by_id or not _active_status(conn, target):
                        raise ValueError(f"retract target 不是本主体的 active unit：{target}")
                    reason = op.get("reason")
                    mus.retract_unit(
                        target, by="model",
                        reason=reason if reason in {"false", "outdated"} else None,
                        actor="consolidation", conn=conn,
                    )
                    summary.retracted.append(target)
                else:
                    raise ValueError(f"未知 consolidation op：{kind}")
            except (ValueError, mus.BoundaryError) as exc:
                summary.skipped.append({"op": op.get("op"), "reason": str(exc)})

    logging_service.log_event(
        "memory_consolidation",
        owner_scope=owner_scope,
        merged=len(summary.merged),
        retracted=len(summary.retracted),
        skipped=len(summary.skipped),
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


# The per-reconcile reflect only covers owners touched by that run, so an owner
# with no fresh evidence would never decay its stale states (the card list keeps
# showing a months-old "current status" as remembered). This daily full sweep is
# the safety net; it is pure SQL, so sweeping every owner costs a few queries.
FULL_SWEEP_INTERVAL_DAYS = 1.0
_FULL_SWEEP_META_KEY = "memory_reflection_full_sweep_ts"


def reflect_all_personas_if_due(
    *, now: float | None = None, interval_days: float = FULL_SWEEP_INTERVAL_DAYS
) -> list[ReflectionSummary]:
    """Run the full-owner reflect sweep at most once per interval (meta-gated)."""
    now = db.now_ts() if now is None else now
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (_FULL_SWEEP_META_KEY,))
    try:
        last = float(row["value"]) if row is not None else 0.0
    except (TypeError, ValueError):
        last = 0.0
    if now - last < interval_days * DAY_SECONDS:
        return []
    summaries = reflect_all_personas(now=now)
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (_FULL_SWEEP_META_KEY, str(now)),
    )
    return summaries


# --- consolidation scheduling (P2b on a slow clock) --------------------------
#
# consolidate_persona feeds an owner's ENTIRE active unit set to one LLM call,
# and its verdicts merge/retract live memories — expensive and user-visible if
# wrong. So the automatic trigger is gated three ways (enough units to be worth
# it / cooldown / something actually changed) and the runner consolidates at
# most ONE most-overdue owner per reconcile job, hard-capping cost and latency.
CONSOLIDATION_MIN_UNITS = 12
CONSOLIDATION_COOLDOWN_DAYS = 3.0
_CONSOLIDATION_META_PREFIX = "memory_consolidation_last_ts:"


def _last_consolidated_ts(owner_scope: str) -> float:
    row = db.query_one(
        "SELECT value FROM meta WHERE key = ?",
        (_CONSOLIDATION_META_PREFIX + owner_scope,),
    )
    try:
        return float(row["value"]) if row is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _set_last_consolidated_ts(owner_scope: str, ts: float) -> None:
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (_CONSOLIDATION_META_PREFIX + owner_scope, str(ts)),
    )


def pick_consolidation_owner(
    *,
    now: float | None = None,
    min_units: int = CONSOLIDATION_MIN_UNITS,
    cooldown_days: float = CONSOLIDATION_COOLDOWN_DAYS,
) -> str | None:
    """The most-overdue owner passing all three gates, or None.

    Gates: at least ``min_units`` active units (smaller sets hold no duplicates
    worth an LLM call), cooldown elapsed since that owner's last consolidation
    attempt, and at least one unit changed since then (no new material, no new
    duplicates). Most-overdue = smallest last-consolidated timestamp, so
    never-consolidated owners go first and attention rotates fairly."""
    now = db.now_ts() if now is None else now
    rows = db.query_all(
        """
        SELECT owner_scope, MAX(updated_at) AS last_change
        FROM memory_units
        WHERE status = 'active'
        GROUP BY owner_scope
        HAVING COUNT(*) >= ?
        ORDER BY owner_scope
        """,
        (min_units,),
    )
    best: str | None = None
    best_last = 0.0
    for row in rows:
        owner = str(row["owner_scope"])
        last = _last_consolidated_ts(owner)
        if now - last < cooldown_days * DAY_SECONDS:
            continue
        if float(row["last_change"]) <= last:
            continue
        if best is None or last < best_last:
            best, best_last = owner, last
    return best


def consolidate_due_owner(
    *,
    producer,
    now: float | None = None,
    min_units: int = CONSOLIDATION_MIN_UNITS,
    cooldown_days: float = CONSOLIDATION_COOLDOWN_DAYS,
) -> ConsolidationSummary | None:
    """Consolidate the one most-overdue owner, if any is due.

    The cooldown stamp is written only after a successful pass: a producer
    failure leaves the owner due, so it is retried on a later run instead of
    silently skipping a whole cooldown window."""
    owner_scope = pick_consolidation_owner(
        now=now, min_units=min_units, cooldown_days=cooldown_days
    )
    if owner_scope is None:
        return None
    summary = consolidate_persona(owner_scope, producer=producer)
    _set_last_consolidated_ts(owner_scope, db.now_ts() if now is None else now)
    return summary
