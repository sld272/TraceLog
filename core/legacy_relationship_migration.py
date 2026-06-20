"""Verify legacy SOUL-memory candidates against current raw user evidence."""

from __future__ import annotations

import json
import re
import sqlite3

from core import db, memory_events_service as mes, memory_unit_service as mus

EVIDENCE_LIMIT = 16


def _metadata(row: sqlite3.Row) -> dict:
    try:
        value = json.loads(row["metadata"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _current_user_events_for_soul(soul_name: str) -> list[sqlite3.Row]:
    owner_scope = f"soul:{soul_name}"
    return db.query_all(
        """
        SELECT e.*
        FROM memory_ingest_events e
        WHERE e.owner_scope = ?
          AND e.author = 'user'
          AND e.op != 'delete'
          AND TRIM(COALESCE(e.content_snapshot, '')) != ''
          AND (
                e.visibility_scope LIKE 'thread:%'
                OR e.visibility_scope = ?
          )
          AND e.id = (
              SELECT latest.id
              FROM memory_ingest_events latest
              WHERE latest.source_type = e.source_type
                AND latest.source_id = e.source_id
              ORDER BY latest.source_revision DESC, latest.id DESC
              LIMIT 1
          )
        ORDER BY e.id DESC
        """,
        (owner_scope, f"private:soul:{soul_name}"),
    )


def _terms(text: str) -> set[str]:
    normalized = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", normalized))
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        terms.update(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    return terms


def _semantic_source_ranks(content: str, soul_name: str) -> dict[tuple[str, str], int]:
    try:
        from core import vectorstore
    except Exception:
        return {}
    ranks: dict[tuple[str, str], int] = {}
    specs = (
        ("comment", "comment_id", "comment_message"),
        ("chat", "message_id", "chat_message"),
    )
    for doc_type, id_field, source_type in specs:
        try:
            hits = vectorstore.query_documents(
                content,
                n_results=EVIDENCE_LIMIT,
                where={
                    "$and": [
                        {"type": {"$eq": doc_type}},
                        {"soul_name": {"$eq": soul_name}},
                        {"role": {"$eq": "user"}},
                    ]
                },
            )
        except Exception:
            continue
        for hit in hits:
            metadata = getattr(hit, "metadata", None) or {}
            source_id = metadata.get(id_field)
            if source_id is None:
                continue
            key = (source_type, str(source_id))
            ranks.setdefault(key, int(getattr(hit, "rank", len(ranks) + 1)))
    return ranks


def evidence_for_candidate(
    candidate: sqlite3.Row,
    *,
    limit: int = EVIDENCE_LIMIT,
) -> tuple[list[dict], int]:
    """Rank current user evidence for one legacy candidate; return context + watermark."""
    soul_name = str(candidate["owner_scope"])[len("soul:"):]
    rows = _current_user_events_for_soul(soul_name)
    max_event_id = max((int(row["id"]) for row in rows), default=0)
    candidate_terms = _terms(str(candidate["content"]))
    semantic_ranks = _semantic_source_ranks(str(candidate["content"]), soul_name)

    def score(row: sqlite3.Row) -> tuple[int, float, int]:
        overlap = len(candidate_terms & _terms(str(row["content_snapshot"] or "")))
        semantic_rank = semantic_ranks.get(
            (str(row["source_type"]), str(row["source_id"]))
        )
        semantic_score = (
            1.0 / (1 + semantic_rank)
            if semantic_rank is not None
            else 0.0
        )
        return overlap, semantic_score, int(row["id"])

    matching = [
        row
        for row in rows
        if score(row)[0] > 0
        or (str(row["source_type"]), str(row["source_id"])) in semantic_ranks
    ]
    selected = sorted(matching or rows[:8], key=score, reverse=True)[:limit]
    evidence: list[dict] = []
    for row in selected:
        item = dict(row)
        item["conversation_context"] = mes.conversation_context_for_event(row)
        evidence.append(item)
    return evidence, max_event_id


def list_due_candidates(limit: int = 50) -> list[sqlite3.Row]:
    rows = db.query_all(
        """
        SELECT *
        FROM memory_units
        WHERE source = 'migrated'
          AND status = 'pending'
          AND type = 'relationship'
          AND owner_scope LIKE 'soul:%'
        ORDER BY created_at ASC, id ASC
        """
    )
    due: list[sqlite3.Row] = []
    event_watermarks: dict[str, int] = {}
    for row in rows:
        soul_name = str(row["owner_scope"])[len("soul:"):]
        if soul_name not in event_watermarks:
            current = _current_user_events_for_soul(soul_name)
            event_watermarks[soul_name] = max(
                (int(event["id"]) for event in current),
                default=0,
            )
        metadata = _metadata(row)
        if "migration_last_review_event_id" not in metadata:
            due.append(row)
        elif event_watermarks[soul_name] > int(
            metadata.get("migration_last_review_event_id") or 0
        ):
            due.append(row)
        if len(due) >= int(limit):
            break
    return due


def has_due_candidates() -> bool:
    return bool(list_due_candidates(limit=1))


def _write_metadata(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    max_event_id: int,
    decision: str,
    resolved_unit_id: str | None = None,
) -> dict:
    metadata = _metadata(row)
    metadata.update(
        {
            "migration_last_review_event_id": int(max_event_id),
            "migration_last_decision": decision,
        }
    )
    if resolved_unit_id is not None:
        metadata["migration_resolved_unit_id"] = resolved_unit_id
    conn.execute(
        "UPDATE memory_units SET metadata = ?, updated_at = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False), db.now_ts(), row["id"]),
    )
    return metadata


def apply_decision(
    unit_id: str,
    *,
    expected_updated_at: float,
    decision: str,
    max_event_id: int,
    evidence_event_ids: list[int] | None = None,
    content: str | None = None,
    confidence: float = 0.85,
    importance: float = 0.8,
) -> str | None:
    """Resolve one migrated candidate. Returns the new active unit id, if any."""
    if decision not in {"confirm", "revise", "retract", "defer"}:
        raise ValueError(f"非法 legacy migration decision：{decision}")
    event_ids = list(dict.fromkeys(int(item) for item in (evidence_event_ids or [])))
    with db.immediate_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM memory_units WHERE id = ?",
            (unit_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"legacy migration candidate 不存在：{unit_id}")
        if (
            row["source"] != "migrated"
            or row["status"] != "pending"
            or float(row["updated_at"]) != float(expected_updated_at)
        ):
            return None

        if decision == "defer":
            before = dict(row)
            metadata = _write_metadata(
                conn,
                row,
                max_event_id=max_event_id,
                decision=decision,
            )
            _record_op(conn, row, "migrate_defer", before, metadata)
            return None

        if decision == "retract":
            mus.retract_unit(
                unit_id,
                by="migration",
                reason="false",
                actor="legacy_relationship_migration",
                conn=conn,
            )
            current = conn.execute(
                "SELECT * FROM memory_units WHERE id = ?",
                (unit_id,),
            ).fetchone()
            _write_metadata(
                conn,
                current,
                max_event_id=max_event_id,
                decision=decision,
            )
            return None

        events = _validated_events(conn, row, event_ids, decision)
        visibility_scope = str(events[0]["visibility_scope"])
        body = str(content or row["content"]).strip()
        if not body:
            raise ValueError("legacy migration 关系记忆内容不能为空")
        new_unit_id = mus.add_unit(
            owner_scope=str(row["owner_scope"]),
            visibility_scope=visibility_scope,
            source_channel="comment" if visibility_scope.startswith("thread:") else "chat",
            type="relationship",
            content=body,
            confidence=max(0.82, min(1.0, float(confidence))),
            evidence_event_ids=event_ids,
            tier="core",
            importance=max(0.70, min(1.0, float(importance))),
            source="reflected",
            actor="legacy_relationship_migration",
            conn=conn,
        )
        before = dict(row)
        conn.execute(
            """
            UPDATE memory_units
            SET status = 'superseded', superseded_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_unit_id, db.now_ts(), unit_id),
        )
        current = conn.execute(
            "SELECT * FROM memory_units WHERE id = ?",
            (unit_id,),
        ).fetchone()
        metadata = _write_metadata(
            conn,
            current,
            max_event_id=max_event_id,
            decision=decision,
            resolved_unit_id=new_unit_id,
        )
        _record_op(
            conn,
            row,
            "migrate_confirm" if decision == "confirm" else "migrate_revise",
            before,
            metadata,
            related_unit_id=new_unit_id,
        )
        return new_unit_id


def _validated_events(
    conn: sqlite3.Connection,
    candidate: sqlite3.Row,
    event_ids: list[int],
    decision: str,
) -> list[sqlite3.Row]:
    if not event_ids:
        raise ValueError(f"{decision} 必须引用至少一条用户 evidence")
    placeholders = ",".join("?" for _ in event_ids)
    events = conn.execute(
        f"""
        SELECT *
        FROM memory_ingest_events
        WHERE id IN ({placeholders})
          AND author = 'user'
          AND op != 'delete'
        ORDER BY id ASC
        """,
        tuple(event_ids),
    ).fetchall()
    if len(events) != len(event_ids):
        raise ValueError("legacy migration 引用了不存在或无效的 evidence")
    owner_scope = str(candidate["owner_scope"])
    visibility_scope = str(events[0]["visibility_scope"])
    if any(
        str(event["owner_scope"]) != owner_scope
        or str(event["visibility_scope"]) != visibility_scope
        for event in events
    ):
        raise ValueError("legacy migration evidence 必须来自当前 SOUL 的同一 bucket")
    for event in events:
        latest = conn.execute(
            """
            SELECT id, op, author
            FROM memory_ingest_events
            WHERE source_type = ? AND source_id = ?
            ORDER BY source_revision DESC, id DESC
            LIMIT 1
            """,
            (event["source_type"], event["source_id"]),
        ).fetchone()
        if (
            latest is None
            or int(latest["id"]) != int(event["id"])
            or latest["op"] == "delete"
            or latest["author"] != "user"
        ):
            raise ValueError("legacy migration 只能引用当前有效的用户 evidence")
    return events


def _record_op(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    op: str,
    before: dict,
    metadata: dict,
    *,
    related_unit_id: str | None = None,
) -> None:
    after = dict(
        conn.execute(
            "SELECT * FROM memory_units WHERE id = ?",
            (row["id"],),
        ).fetchone()
    )
    after["metadata"] = metadata
    conn.execute(
        """
        INSERT INTO memory_unit_ops(
            unit_id, related_unit_id, op, actor, before_json, after_json, created_at
        ) VALUES (?, ?, ?, 'migration', ?, ?, ?)
        """,
        (
            row["id"],
            related_unit_id,
            op,
            json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
            db.now_ts(),
        ),
    )
