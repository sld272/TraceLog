"""Evidence feedback persistence for retrieval calibration."""

from __future__ import annotations

from dataclasses import dataclass

from core import db, logging_service

VALID_CHANNELS = {"chat", "comment", "public_post"}
VALID_VERDICTS = {"irrelevant"}


@dataclass(frozen=True)
class EvidenceFeedback:
    id: int | None
    channel: str
    message_id: int
    doc_id: str
    verdict: str
    created_at: float
    created: bool


def record_feedback(
    *,
    channel: str,
    message_id: int,
    doc_id: str,
    verdict: str = "irrelevant",
) -> EvidenceFeedback:
    """Persist one idempotent evidence feedback marker."""
    clean_channel = str(channel or "").strip()
    clean_doc_id = str(doc_id or "").strip()
    clean_verdict = str(verdict or "irrelevant").strip()
    numeric_message_id = int(message_id)
    if clean_channel not in VALID_CHANNELS:
        raise ValueError(f"非法反馈渠道：{clean_channel}")
    if numeric_message_id <= 0:
        raise ValueError("message_id 必须为正整数")
    if not clean_doc_id:
        raise ValueError("doc_id 不能为空")
    if clean_verdict not in VALID_VERDICTS:
        raise ValueError(f"非法反馈类型：{clean_verdict}")

    now = db.now_ts()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO evidence_feedback(channel, message_id, doc_id, verdict, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean_channel, numeric_message_id, clean_doc_id, clean_verdict, now),
        )
        created = cursor.rowcount > 0
        row = conn.execute(
            """
            SELECT id, channel, message_id, doc_id, verdict, created_at
            FROM evidence_feedback
            WHERE channel = ? AND message_id = ? AND doc_id = ?
            """,
            (clean_channel, numeric_message_id, clean_doc_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("evidence feedback write failed")
    if created:
        logging_service.log_event(
            "evidence_feedback",
            channel=clean_channel,
            message_id=numeric_message_id,
            doc_id=clean_doc_id,
            verdict=clean_verdict,
        )
    return EvidenceFeedback(
        id=int(row["id"]),
        channel=str(row["channel"]),
        message_id=int(row["message_id"]),
        doc_id=str(row["doc_id"]),
        verdict=str(row["verdict"]),
        created_at=float(row["created_at"]),
        created=created,
    )
