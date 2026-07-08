"""SOUL proactive revisit (回访) — the only exit for cross-bucket contradictions.

Red line #1 forbids private evidence from silently rewriting public memory, so
a contested mark can only dissolve through fresh evidence the user personally
gives. This module creates the opening for that evidence: when a private-chat
reply is being assembled and the conversation already touches a contested
topic, ONE gentle revisit directive may ride along.

The ladder keeps it from feeling like an interrogation:

  * rung 1 — just ask how things are going ("最近比赛准备得怎么样？"). The fresh
    answer flows back through the ordinary chat-evidence -> reconcile path and
    most contradictions dissolve without ever being pointed out.
  * rung 2 — only if a previous revisit didn't settle it: gently verify whether
    the remembered claim still holds, offering four light reply directions
    (以最新说法为准 / 有新变化 / 只是随口一说 / 两边都对，场合不同). Quoting or
    contrasting what the user said elsewhere ("你广场上说 X 但私下说 Y") is
    banned outright.

Restraints: private chat only; only when the contested topic actually surfaced
in this reply's retrieved memory (relevance gate); at most one directive per
reply; per-unit rate limit; global opt-out. Attempts are recorded in the
contested unit's metadata JSON.
"""

from __future__ import annotations

import json

import sqlite3

from core import db, memory_unit_service as mus

REVISIT_MIN_INTERVAL_SECONDS = 3 * 86400.0  # same-topic follow-up: once per window
REVISIT_OPTOUT_KEY = "memory_revisit_optout"


def revisit_enabled() -> bool:
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (REVISIT_OPTOUT_KEY,))
    return row is None or str(row["value"]) != "1"


def set_revisit_enabled(enabled: bool) -> None:
    """The「以后不用问这类差异」switch: opting out stops every future revisit
    directive; contested marks then dissolve only via spontaneous evidence."""
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (REVISIT_OPTOUT_KEY, "0" if enabled else "1"),
    )


def _revisit_state(row: sqlite3.Row) -> dict:
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except (TypeError, ValueError):
        metadata = {}
    state = metadata.get("revisit") if isinstance(metadata, dict) else None
    return state if isinstance(state, dict) else {}


def _record_attempt(row: sqlite3.Row, now: float) -> int:
    """Bump the unit's revisit attempt counter in its metadata JSON; returns the
    attempt count BEFORE this one (0 = this is the first, rung 1)."""
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    state = metadata.get("revisit")
    if not isinstance(state, dict):
        state = {}
    previous_attempts = int(state.get("attempts") or 0)
    state.update({"attempts": previous_attempts + 1, "last_ts": now})
    metadata["revisit"] = state
    db.execute(
        "UPDATE memory_units SET metadata = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False), row["id"]),
    )
    return previous_attempts


def _candidates(reply_soul: str, retrieved_unit_ids: list[str]) -> list[sqlite3.Row]:
    """Contested units whose contradiction involves THIS soul's private bucket
    and whose topic surfaced in this reply (either end retrieved). Only the
    soul that holds the private side may revisit — another soul asking would
    itself leak that something is off."""
    if not retrieved_unit_ids:
        return []
    placeholders = ",".join("?" for _ in retrieved_unit_ids)
    return db.query_all(
        f"""
        SELECT DISTINCT u.*
        FROM memory_units u
        JOIN memory_unit_links l
          ON (l.a_unit_id = u.id OR l.b_unit_id = u.id)
         AND l.relation = 'contradicts'
        JOIN memory_units other
          ON other.id = CASE WHEN l.a_unit_id = u.id THEN l.b_unit_id ELSE l.a_unit_id END
        WHERE u.contested_at IS NOT NULL
          AND u.status = 'active'
          AND other.owner_scope = ?
          AND (u.id IN ({placeholders}) OR other.id IN ({placeholders}))
        """,
        (f"soul:{reply_soul}", *retrieved_unit_ids, *retrieved_unit_ids),
    )


def revisit_directive(
    channel: str,
    reply_soul: str | None,
    retrieved_unit_ids: list[str],
    *,
    now: float | None = None,
) -> str:
    """The [回访] prompt block for this reply, or '' when no revisit is due.

    Recording the attempt happens here (the directive WILL reach the model), so
    the rate limit holds even if the model chooses not to act on it — better to
    under-ask than to nag."""
    if channel != "chat" or not reply_soul:
        return ""
    if not revisit_enabled():
        return ""
    now = db.now_ts() if now is None else now

    due = None
    for row in _candidates(reply_soul, retrieved_unit_ids):
        state = _revisit_state(row)
        last_ts = float(state.get("last_ts") or 0.0)
        if now - last_ts < REVISIT_MIN_INTERVAL_SECONDS:
            continue
        if due is None or last_ts < float(_revisit_state(due).get("last_ts") or 0.0):
            due = row
    if due is None:
        return ""

    previous_attempts = _record_attempt(due, now)
    topic = str(due["content"])
    if previous_attempts == 0:
        return (
            "[回访]\n"
            f"你记得「{topic}」，但想更新一下近况。如果本轮对话的话题自然合适，"
            "顺带关心一句这方面最近怎么样了（例如问问进展/感受）。"
            "只是自然的关心：不要提到任何记忆、差异或你为什么想问；"
            "话题不合适就完全不问，等下次。"
        )
    return (
        "[回访]\n"
        f"你之前问过近况，但「{topic}」现在是否仍然成立还不明朗。"
        "如果话题自然合适，温和地和用户确认一下现在的情况，并给出台阶——"
        "可以顺带说明怎么答都行，比如：以你现在说的为准 / 情况有了新变化 / "
        "之前只是随口一说 / 两种说法都对，只是场合不同。"
        "铁律：绝不引用或对比用户在其他场合说过的话（不许出现"
        "「你之前说过…但…」式的对质），用户怎么答都接住，不追问。"
    )
