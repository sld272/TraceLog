"""Cross-bucket duplicate/contradiction crosslink pass (P1: link, never merge).

Buckets (owner × visibility) are the privacy backbone and stay isolated;
consolidation/supersede refuse to cross them by design. But the same fact — or
a contradiction — routinely lands in two buckets: the user says "在考研" in a
public post AND in a private chat. This low-frequency pass, riding the tail of
each reconcile run, only RECORDS the relation:

    recently touched units --(exact content match + unit-vector neighbours)-->
    candidate cross-bucket pairs --(one batched LLM judgment)-->
    memory_unit_links rows (same_fact / contradicts / context_variant)

For ``contradicts`` the more-public side gets an attribution-free
``contested_at`` mark: read paths hedge it, the portrait drops it, and nothing
anywhere says WHY (the reason may only ever surface inside a private revisit).
No content moves, no bucket boundary weakens, public memory itself changes only
through public/user evidence — the two red lines hold.

A meta-table cursor bounds each scan to units touched since the last pass.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlite3

from core import db, logging_service, memory_unit_service as mus

# Candidate recall: cosine floor for a cross-bucket vector neighbour to be worth
# judging. Deliberately loose — the LLM verdict is the precision stage.
CROSSLINK_CANDIDATE_SIM = 0.60
CROSSLINK_MAX_NEIGHBORS = 3       # per source unit
CROSSLINK_MAX_SOURCE_UNITS = 20   # per pass
CROSSLINK_MAX_PAIRS = 12          # per LLM call / pass
CROSSLINK_MAX_STALE_LINKS = 8     # re-judged per pass

# stored key string predates the crosslink rename; keep it so the scan cursor survives
_META_KEY = "memory_linker_last_scan_ts"


@dataclass(frozen=True)
class CrosslinkResult:
    scanned: int
    judged_pairs: int
    linked: int
    contested: int


def _last_scan_ts() -> float:
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (_META_KEY,))
    try:
        return float(row["value"]) if row is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _set_last_scan_ts(ts: float) -> None:
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (_META_KEY, str(ts))
    )


def _recent_units(since: float) -> list[sqlite3.Row]:
    return db.query_all(
        """
        SELECT * FROM memory_units
        WHERE status = 'active' AND updated_at > ? AND type != 'goal'
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (since, CROSSLINK_MAX_SOURCE_UNITS),
    )


def _same_bucket(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    return (
        str(a["owner_scope"]) == str(b["owner_scope"])
        and str(a["visibility_scope"]) == str(b["visibility_scope"])
    )


def _candidate_rows(source: sqlite3.Row) -> list[sqlite3.Row]:
    """Cross-bucket candidates for one source unit: exact content twins plus
    loose vector neighbours. Recall stage only — precision is the LLM's job."""
    out: list[sqlite3.Row] = []
    seen: set[str] = {str(source["id"])}

    for row in db.query_all(
        """
        SELECT * FROM memory_units
        WHERE status = 'active' AND id != ? AND TRIM(content) = TRIM(?)
        """,
        (source["id"], source["content"]),
    ):
        if _same_bucket(source, row) or str(row["id"]) in seen:
            continue
        seen.add(str(row["id"]))
        out.append(row)

    try:
        from core import vectorstore

        hits = vectorstore.query_documents(
            str(source["content"]), n_results=8, where={"type": "unit"}
        )
    except Exception:
        hits = []
    neighbors = 0
    for hit in hits:
        if neighbors >= CROSSLINK_MAX_NEIGHBORS:
            break
        distance = getattr(hit, "distance", None)
        if distance is None or (1.0 - float(distance)) < CROSSLINK_CANDIDATE_SIM:
            continue
        meta = getattr(hit, "metadata", None) or {}
        uid = str(meta.get("unit_id") or "")
        if not uid or uid in seen:
            continue
        row = db.query_one(
            "SELECT * FROM memory_units WHERE id = ? AND status = 'active'", (uid,)
        )
        if row is None or _same_bucket(source, row):
            continue
        seen.add(uid)
        out.append(row)
        neighbors += 1
    return out


def _layer_label(visibility_scope: str) -> str:
    """Coarse 公开/私聊 label for the judging prompt — the raw scope string
    (which carries the soul name) never reaches the LLM."""
    return "私聊" if str(visibility_scope).startswith("private:") else "公开"


def _more_public_side(a: sqlite3.Row, b: sqlite3.Row) -> sqlite3.Row:
    """The side a contradiction mark lands on: lower visibility rank loses;
    equal ranks fall back to the staler (older last_confirmed) side — the newer
    record is presumed the fresher truth."""
    rank_a = mus.visibility_rank(str(a["visibility_scope"]))
    rank_b = mus.visibility_rank(str(b["visibility_scope"]))
    if rank_a != rank_b:
        return a if rank_a < rank_b else b
    return a if float(a["last_confirmed"]) <= float(b["last_confirmed"]) else b


def _clear_contested_ends(a: sqlite3.Row, b: sqlite3.Row) -> None:
    for row in (a, b):
        if row["contested_at"]:
            mus.clear_contested(str(row["id"]))


def maintain_links(judge) -> int:
    """Re-judge links whose ends changed since the verdict (P1 closed loop).

    This is how a revisit answer lands: the user's reply becomes chat evidence,
    reconcile confirms/revises the unit (bumping last_confirmed past the link's
    created_at), and this step re-judges the pair with the CURRENT contents —
    a dissolved contradiction downgrades to same_fact/context_variant/unrelated
    and the contested mark comes off; a still-standing one re-marks. Links with
    a dead end (retracted/dormant/superseded) are dropped outright and their
    contested marks cleared: the basis for hedging is gone."""
    rows = db.query_all(
        """
        SELECT l.id AS link_id, l.a_unit_id, l.b_unit_id, l.relation, l.created_at
        FROM memory_unit_links l
        JOIN memory_units a ON a.id = l.a_unit_id
        JOIN memory_units b ON b.id = l.b_unit_id
        WHERE a.status != 'active' OR b.status != 'active'
           OR MAX(a.last_confirmed, b.last_confirmed) > l.created_at
        ORDER BY l.created_at ASC
        LIMIT ?
        """,
        (CROSSLINK_MAX_STALE_LINKS,),
    )
    if not rows:
        return 0

    handled = 0
    to_judge: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    for link in rows:
        a = db.query_one("SELECT * FROM memory_units WHERE id = ?", (link["a_unit_id"],))
        b = db.query_one("SELECT * FROM memory_units WHERE id = ?", (link["b_unit_id"],))
        if a is None or b is None:
            continue
        if a["status"] != "active" or b["status"] != "active":
            db.execute("DELETE FROM memory_unit_links WHERE id = ?", (link["link_id"],))
            _clear_contested_ends(a, b)
            handled += 1
            continue
        to_judge.append((a, b))

    if not to_judge:
        return handled
    payload = [
        {
            "a": {"unit_id": str(a["id"]), "content": str(a["content"]),
                  "layer": _layer_label(a["visibility_scope"])},
            "b": {"unit_id": str(b["id"]), "content": str(b["content"]),
                  "layer": _layer_label(b["visibility_scope"])},
        }
        for a, b in to_judge
    ]
    verdicts = judge(payload)
    if verdicts is None:
        return handled  # links stay stale; retried next pass
    rows_by_id = {str(r["id"]): r for pair in to_judge for r in pair}
    judged_keys = {tuple(sorted((str(a["id"]), str(b["id"])))) for a, b in to_judge}
    for verdict in verdicts:
        a_id, b_id = str(verdict.get("a")), str(verdict.get("b"))
        relation = str(verdict.get("relation"))
        key = tuple(sorted((a_id, b_id)))
        if key not in judged_keys:
            continue
        a, b = rows_by_id[a_id], rows_by_id[b_id]
        if relation == "unrelated":
            db.execute(
                "DELETE FROM memory_unit_links WHERE a_unit_id = ? AND b_unit_id = ?",
                (key[0], key[1]),
            )
            _clear_contested_ends(a, b)
        elif relation == "contradicts":
            mus.add_unit_link(a_id, b_id, relation)  # refreshes created_at
            target = _more_public_side(a, b)
            if not target["contested_at"]:
                mus.mark_contested(str(target["id"]))
        else:  # same_fact / context_variant — contradiction dissolved
            mus.add_unit_link(a_id, b_id, relation)
            _clear_contested_ends(a, b)
        handled += 1
    return handled


def run_crosslink_pass(
    client,
    model: str,
    *,
    judge=None,
    trace_context: dict | None = None,
) -> CrosslinkResult:
    """One bounded crosslink pass. ``judge`` may be injected for tests: it takes
    the pair payload and returns [{"a","b","relation"}] (see
    call_memory_link_judge). On judge failure the scan cursor stays put so the
    same units are retried next run."""
    if judge is None:
        from core.llm import memory_router

        def judge(payload):  # noqa: A001 - deliberate shadow, single call path
            return memory_router.call_memory_link_judge(
                client, model, pairs=payload, trace_context=trace_context
            )

    # maintenance first: links whose ends changed (e.g. a revisit answer flowed
    # back) get re-judged before any new discovery
    maintain_links(judge)

    scan_started = db.now_ts()
    sources = _recent_units(_last_scan_ts())
    if not sources:
        return CrosslinkResult(scanned=0, judged_pairs=0, linked=0, contested=0)

    rows_by_id: dict[str, sqlite3.Row] = {}
    pairs: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for source in sources:
        for candidate in _candidate_rows(source):
            key = tuple(sorted((str(source["id"]), str(candidate["id"]))))
            if key in seen_pairs or mus.linked_pair_exists(*key):
                continue
            seen_pairs.add(key)
            rows_by_id[str(source["id"])] = source
            rows_by_id[str(candidate["id"])] = candidate
            pairs.append((source, candidate))
            if len(pairs) >= CROSSLINK_MAX_PAIRS:
                break
        if len(pairs) >= CROSSLINK_MAX_PAIRS:
            break

    def _advance_cursor() -> None:
        if len(sources) >= CROSSLINK_MAX_SOURCE_UNITS:
            # bounded batch: continue from the last processed unit next run
            _set_last_scan_ts(max(float(r["updated_at"]) for r in sources))
        else:
            _set_last_scan_ts(scan_started)

    if not pairs:
        _advance_cursor()
        return CrosslinkResult(scanned=len(sources), judged_pairs=0, linked=0, contested=0)

    payload = [
        {
            "a": {
                "unit_id": str(a["id"]),
                "content": str(a["content"]),
                "layer": _layer_label(a["visibility_scope"]),
            },
            "b": {
                "unit_id": str(b["id"]),
                "content": str(b["content"]),
                "layer": _layer_label(b["visibility_scope"]),
            },
        }
        for a, b in pairs
    ]
    verdicts = judge(payload)
    if verdicts is None:
        # leave the cursor: these units are re-scanned next run
        return CrosslinkResult(scanned=len(sources), judged_pairs=len(pairs), linked=0, contested=0)

    linked = 0
    contested = 0
    for verdict in verdicts:
        a_id, b_id = str(verdict.get("a")), str(verdict.get("b"))
        relation = str(verdict.get("relation"))
        key = tuple(sorted((a_id, b_id)))
        if key not in seen_pairs:  # the model may not invent pairs
            continue
        if relation == "unrelated":
            continue
        try:
            mus.add_unit_link(a_id, b_id, relation)
        except ValueError:
            continue
        linked += 1
        if relation == "contradicts":
            target = _more_public_side(rows_by_id[a_id], rows_by_id[b_id])
            if not target["contested_at"]:
                mus.mark_contested(str(target["id"]))
                contested += 1

    _advance_cursor()
    result = CrosslinkResult(
        scanned=len(sources), judged_pairs=len(pairs), linked=linked, contested=contested
    )
    logging_service.log_event(
        "memory_crosslink_pass",
        level="DEBUG",
        scanned=result.scanned,
        judged_pairs=result.judged_pairs,
        linked=result.linked,
        contested=result.contested,
    )
    return result
