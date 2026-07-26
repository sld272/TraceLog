"""Pure-SQL gates and source selection for proactive SOUL letters."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from core import chat_service, db, logging_service, soul_service
from core.cli.config import normalize_proactive_message_config
from core.llm.types import LLMClient

DAY_SECONDS = 86_400.0
SCAN_COOLDOWN_SECONDS = DAY_SECONDS
SOUL_COOLDOWN_SECONDS = 7 * DAY_SECONDS
GLOBAL_COOLDOWN_SECONDS = 3 * DAY_SECONDS
PROACTIVE_MESSAGE_DISABLED_ENV = "PROACTIVE_MESSAGE_DISABLED"
LAST_SCAN_META_KEY = "soul_proactive_last_scan_at"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on", "enabled"}


@dataclass(frozen=True)
class ProactiveScanDecision:
    should_call_llm: bool
    reason: str
    candidate_souls: tuple[str, ...] = ()
    last_user_activity_at: float | None = None
    silent_for_days: int | None = None

    @property
    def soul_name(self) -> str | None:
        return self.candidate_souls[0] if self.candidate_souls else None


def env_disabled() -> bool:
    value = os.environ.get(PROACTIVE_MESSAGE_DISABLED_ENV, "")
    return value.strip().lower() in _TRUE_ENV_VALUES


def proactive_message_enabled(config: dict[str, Any]) -> bool:
    settings = normalize_proactive_message_config(
        config.get("proactive_message")
    )
    return bool(settings["enabled"]) and not env_disabled()


def scan_for_candidates(
    config: dict[str, Any],
    *,
    now: float,
) -> ProactiveScanDecision:
    """Claim a due daily scan and return SOULs ordered by letter priority."""
    settings = normalize_proactive_message_config(
        config.get("proactive_message")
    )
    if not settings["enabled"]:
        return ProactiveScanDecision(False, "config_disabled")
    if env_disabled():
        return ProactiveScanDecision(False, "env_disabled")

    with db.immediate_transaction() as conn:
        last_scan = conn.execute(
            "SELECT CAST(value AS REAL) AS ts FROM meta WHERE key = ?",
            (LAST_SCAN_META_KEY,),
        ).fetchone()
        if (
            last_scan is not None
            and float(last_scan["ts"]) > now - SCAN_COOLDOWN_SECONDS
        ):
            return ProactiveScanDecision(False, "scan_cooldown")

        conn.execute(
            """
            INSERT INTO meta(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (LAST_SCAN_META_KEY, str(now)),
        )

        candidates = conn.execute(
            """
            WITH last_letters AS (
                SELECT chat_threads.soul_name, MAX(soul_letters.sent_at) AS last_sent_at
                FROM soul_letters
                JOIN chat_messages
                  ON chat_messages.id = soul_letters.message_id
                JOIN chat_threads
                  ON chat_threads.id = chat_messages.thread_id
                GROUP BY chat_threads.soul_name
            )
            SELECT souls.name, last_letters.last_sent_at
            FROM souls
            LEFT JOIN last_letters ON last_letters.soul_name = souls.name
            WHERE souls.enabled = 1
              AND (
                  last_letters.last_sent_at IS NULL
                  OR last_letters.last_sent_at <= ?
              )
            ORDER BY
                CASE WHEN last_letters.last_sent_at IS NULL THEN 0 ELSE 1 END,
                last_letters.last_sent_at ASC,
                souls.sort_order ASC,
                souls.name ASC
            """,
            (now - SOUL_COOLDOWN_SECONDS,),
        ).fetchall()
        if not candidates:
            return ProactiveScanDecision(False, "soul_cooldown")

        activity = conn.execute(
            """
            SELECT MAX(created_at) AS last_user_activity_at
            FROM (
                SELECT created_at FROM posts
                UNION ALL
                SELECT created_at FROM comments WHERE role = 'user'
                UNION ALL
                SELECT created_at FROM chat_messages WHERE role = 'user'
            )
            """
        ).fetchone()
        last_user_activity_at = activity["last_user_activity_at"]
        if last_user_activity_at is None:
            return ProactiveScanDecision(False, "no_user_activity")
        last_user_activity_at = float(last_user_activity_at)
        if (
            last_user_activity_at
            > now - int(settings["silence_days"]) * DAY_SECONDS
        ):
            return ProactiveScanDecision(
                False,
                "silence_gate",
                last_user_activity_at=last_user_activity_at,
            )

        global_letter = conn.execute(
            "SELECT MAX(sent_at) AS last_sent_at FROM soul_letters"
        ).fetchone()
        last_sent_at = global_letter["last_sent_at"]
        if (
            last_sent_at is not None
            and float(last_sent_at) > now - GLOBAL_COOLDOWN_SECONDS
        ):
            return ProactiveScanDecision(
                False,
                "global_cooldown",
                last_user_activity_at=last_user_activity_at,
            )

        silent_for_days = int(
            max(0.0, now - last_user_activity_at) // DAY_SECONDS
        )
        return ProactiveScanDecision(
            True,
            "eligible",
            tuple(str(row["name"]) for row in candidates),
            last_user_activity_at=last_user_activity_at,
            silent_for_days=silent_for_days,
        )


def run_proactive_message(
    config: dict[str, Any],
    client: LLMClient,
    model: str,
    *,
    now: float | None = None,
) -> chat_service.ChatMessage | None:
    """Send the first letter produced by the ordered eligible SOULs."""
    from core.llm import soul_letter_router

    current = db.now_ts() if now is None else float(now)
    decision = scan_for_candidates(config, now=current)
    if not decision.should_call_llm:
        return None

    for soul_name in decision.candidate_souls:
        thread = chat_service.get_or_create_thread(soul_name)
        draft = soul_letter_router.call_soul_letter(
            client,
            model,
            soul_name=soul_name,
            persona=soul_service.read_soul_content(soul_name),
            silent_for_days=int(decision.silent_for_days),
            trace_context={
                "channel": "proactive_message",
                "thread_id": thread.id,
            },
        )
        if draft is None:
            continue

        def persist_letter(
            conn: sqlite3.Connection,
            message_id: int,
            sent_at: float,
        ) -> None:
            _insert_soul_letter_rows(
                conn,
                message_id,
                sent_at,
                material_post_ids=draft.material_post_ids,
            )

        message = chat_service.append_unprompted_assistant_message(
            thread.id,
            draft.message,
            metadata={
                "status": "ok",
                "proactive_message": True,
            },
            transaction_hook=persist_letter,
        )
        logging_service.log_event(
            "soul_proactive_message_sent",
            soul_name=soul_name,
            thread_id=thread.id,
            message_id=message.id,
            material_post_count=len(draft.material_post_ids),
        )
        return message
    return None


def run_proactive_message_best_effort(
    config: dict[str, Any],
    client: LLMClient | None,
    model: str | None,
) -> chat_service.ChatMessage | None:
    """Run proactive delivery without allowing it to fail other maintenance."""
    if not proactive_message_enabled(config) or client is None or not model:
        return None
    try:
        return run_proactive_message(config, client, model)
    except Exception as exc:
        logging_service.log_event(
            "soul_proactive_message_failed",
            level="WARNING",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None


def _insert_soul_letter_rows(
    conn: sqlite3.Connection,
    message_id: int,
    sent_at: float,
    *,
    material_post_ids: tuple[str, ...],
) -> None:
    conn.execute(
        """
        INSERT INTO soul_letters(message_id, sent_at)
        VALUES (?, ?)
        """,
        (message_id, sent_at),
    )
    conn.executemany(
        """
        INSERT INTO soul_message_sources(message_id, post_id)
        VALUES (?, ?)
        """,
        [(message_id, post_id) for post_id in material_post_ids],
    )


def own_comments_by_post(soul_name: str) -> dict[str, tuple[str, ...]]:
    """This SOUL's own public replies, keyed by post.

    Handed to the letter prompt so a second reaction to a post it already
    answered is visibly redundant — the silence gate stops a *fresh* post from
    existing, but it cannot stop the model from re-reacting to an old one."""
    rows = db.query_all(
        """
        SELECT post_id, content
        FROM comments
        WHERE role = 'assistant' AND soul_name = ? AND content <> ''
        ORDER BY id ASC
        """,
        (soul_name,),
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["post_id"]), []).append(str(row["content"]))
    return {post_id: tuple(items) for post_id, items in grouped.items()}


def list_unused_public_material_rows(
    *,
    since: float,
) -> list[dict[str, Any]]:
    """Return public posts and user-authored public comments not used by a letter."""
    rows = db.query_all(
        """
        WITH available_posts AS (
            SELECT posts.id, posts.ts, posts.content, posts.created_at
            FROM posts
            WHERE posts.created_at >= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM soul_message_sources
                  WHERE soul_message_sources.post_id = posts.id
              )
        ),
        material_rows AS (
            SELECT
                available_posts.id AS post_id,
                available_posts.ts AS post_ts,
                'post' AS item_kind,
                0 AS item_kind_order,
                available_posts.id AS item_id,
                available_posts.content AS content,
                available_posts.created_at AS item_created_at
            FROM available_posts

            UNION ALL

            SELECT
                available_posts.id AS post_id,
                available_posts.ts AS post_ts,
                'comment' AS item_kind,
                1 AS item_kind_order,
                CAST(comments.id AS TEXT) AS item_id,
                comments.content AS content,
                comments.created_at AS item_created_at
            FROM available_posts
            JOIN comments ON comments.post_id = available_posts.id
            WHERE comments.role = 'user'
        )
        SELECT
            post_id,
            post_ts,
            item_kind,
            item_id,
            content,
            item_created_at
        FROM material_rows
        ORDER BY
            post_ts ASC,
            post_id ASC,
            item_kind_order ASC,
            item_created_at ASC,
            item_id ASC
        """,
        (since,),
    )
    return [dict(row) for row in rows]
