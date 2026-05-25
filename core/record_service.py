"""Post persistence service."""

from __future__ import annotations

import json
from datetime import datetime

from core import db

CONTEXT_POST_COUNT = 3


def save_post(content: str) -> str:
    """Save a post to SQLite, then try to index it in ChromaDB."""
    now = datetime.now().astimezone()
    post_id = _next_post_id(now.strftime("%Y%m%d"))
    body = content.strip()

    db.execute(
        """
        INSERT INTO posts(id, ts, content, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (post_id, now.isoformat(), body, now.timestamp(), now.timestamp()),
    )

    try:
        vectorstore = _vectorstore()
        if not vectorstore.is_initialized():
            raise RuntimeError("vectorstore is not initialized")
        vectorstore.index_post(post_id, body)
        _clear_pending_embedding(post_id)
    except Exception as exc:
        _mark_pending_embedding(post_id, body, str(exc))

    return post_id


def format_post(row) -> str:
    """Format one SQLite post row as markdown with frontmatter."""
    frontmatter = (
        "---\n"
        f"id: \"{row['id']}\"\n"
        f"date: \"{row['ts']}\"\n"
        "type: \"post\"\n"
        "---\n\n"
    )
    return frontmatter + f"\n{row['content']}\n"


def read_recent_posts(count: int = CONTEXT_POST_COUNT) -> str:
    """Read recent posts from SQLite and join them in chronological order."""
    rows = db.query_all(
        """
        SELECT id, ts, content
        FROM posts
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (count,),
    )
    parts = [format_post(row).strip() for row in reversed(rows)]
    return "\n\n---\n\n".join(parts)


def retry_pending_embeddings(limit: int | None = None) -> int:
    """Retry pending ChromaDB indexing jobs. Returns the number fixed."""
    vectorstore = _vectorstore()
    if not vectorstore.is_initialized():
        return 0

    sql = """
        SELECT key, value
        FROM meta
        WHERE key LIKE 'pending_embedding:%'
        ORDER BY key
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)

    fixed = 0
    for row in db.query_all(sql, params):
        try:
            payload = json.loads(row["value"])
            post_id = payload["post_id"]
            content = payload["content"]
            vectorstore.index_post(post_id, content)
            db.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
            fixed += 1
        except Exception:
            continue
    return fixed


def _next_post_id(today: str) -> str:
    row = db.query_one(
        """
        SELECT id
        FROM posts
        WHERE id LIKE ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"{today}-%",),
    )
    if row is None:
        return f"{today}-001"
    try:
        seq = int(str(row["id"]).split("-")[1]) + 1
    except (IndexError, ValueError):
        seq = 1
    return f"{today}-{seq:03d}"


def _mark_pending_embedding(post_id: str, content: str, error: str) -> None:
    payload = {
        "post_id": post_id,
        "content": content,
        "error": error,
        "created_at": db.now_ts(),
    }
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (f"pending_embedding:{post_id}", json.dumps(payload, ensure_ascii=False)),
    )


def _clear_pending_embedding(post_id: str) -> None:
    db.execute("DELETE FROM meta WHERE key = ?", (f"pending_embedding:{post_id}",))


def _vectorstore():
    from core import vectorstore
    return vectorstore
