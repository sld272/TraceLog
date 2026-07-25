"""Preview or apply goal-activity extraction over public history.

The ledger is derived data: every row comes from a post or comment plus a goal,
so this script is also the way to rebuild ``goal_activities`` from the source of
truth if that table is ever lost or corrupted.

In normal operation, though, run ``--apply`` once per workspace. Writes are
idempotent (``UNIQUE`` + ``INSERT OR IGNORE``), but the judgement is not:
candidates sitting on the confidence threshold move between runs, so repeated
applies over an intact table keep sweeping in whichever borderline rows happened
to clear 0.8 that time. The effective threshold degrades into "cleared 0.8 at
least once".
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from openai import OpenAI

from core import db, goal_activity_service, goal_service, logging_service
from core.cli.config import load_config
from core.llm import secondary_model, suggestion_router
from core.system_timezone import SYSTEM_TIMEZONE

BACKFILL_CONFIDENCE_THRESHOLD = 0.8
BACKFILL_ACTIVITY_KINDS = ("commitment", "progress", "blocked", "milestone")
CONTENT_PREVIEW_LENGTH = 48


@dataclass(frozen=True)
class HistoricalMessage:
    evidence_ref: str
    source_type: str
    content: str
    created_at: float


@dataclass(frozen=True)
class BackfillActivity:
    evidence_ref: str
    source_type: str
    created_at: float
    goal_id: str
    goal_title: str
    kind: str
    evidence_span: str
    confidence: float


@dataclass(frozen=True)
class BackfillResult:
    scanned_posts: int
    scanned_comments: int
    activities: tuple[BackfillActivity, ...]
    undetermined_refs: tuple[str, ...]
    write_attempts: int

    @property
    def scanned_total(self) -> int:
        return self.scanned_posts + self.scanned_comments


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="goal activity history backfill")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="写入目标动态台账（每个工作区只跑一次）；默认仅预览",
    )
    return parser.parse_args(argv)


def load_historical_messages() -> list[HistoricalMessage]:
    rows = db.query_all(
        """
        SELECT evidence_ref, source_type, content, created_at
        FROM (
            SELECT
                'post:' || id AS evidence_ref,
                'post' AS source_type,
                content,
                created_at
            FROM posts

            UNION ALL

            SELECT
                'comment:' || CAST(id AS TEXT) AS evidence_ref,
                'comment' AS source_type,
                content,
                created_at
            FROM comments
            WHERE role = 'user'
        )
        ORDER BY created_at ASC, evidence_ref ASC
        """
    )
    return [
        HistoricalMessage(
            evidence_ref=str(row["evidence_ref"]),
            source_type=str(row["source_type"]),
            content=str(row["content"]),
            created_at=float(row["created_at"]),
        )
        for row in rows
    ]


def active_goal_context(goals: list[dict]) -> str:
    if not goals:
        return ""
    return "# 当前目标\n\n" + "\n".join(
        goal_service.format_goal_for_context(goal) for goal in goals
    )


def run_backfill(
    client,
    model: str,
    *,
    apply: bool = False,
    emit: Callable[[str], None] = print,
) -> BackfillResult:
    goals = goal_service.list_goals(status="active")
    goals_by_id = {str(goal["id"]): goal for goal in goals}
    context = active_goal_context(goals)
    messages = load_historical_messages()
    scanned_posts = sum(message.source_type == "post" for message in messages)
    scanned_comments = sum(message.source_type == "comment" for message in messages)

    emit("=== 目标动态历史回填 ===")
    emit(f"模式：{'apply（写入台账）' if apply else 'dry-run（仅预览，不写入台账）'}")
    emit(f"进行中目标：{len(goals)}")
    for goal in goals:
        emit(f"  - [{goal['id']}] {goal['title']}")
    emit(
        f"扫描范围：posts={scanned_posts}，user comments={scanned_comments}，"
        f"合计={len(messages)}"
    )
    emit("")

    selected: list[BackfillActivity] = []
    undetermined_refs: list[str] = []
    for index, message in enumerate(messages, start=1):
        emit(
            f"[{index}/{len(messages)}] {_date_text(message.created_at)} "
            f"{message.evidence_ref} | {_content_preview(message.content)}"
        )
        if not goals:
            emit("  无动态（当前没有进行中的目标）")
            continue

        call_status: dict = {"status": "ok", "error": None}

        def capture_status(status: dict) -> None:
            call_status.update(status)

        candidates = suggestion_router.call_suggestion_router(
            client,
            model,
            user_input=message.content,
            context=context,
            trace_context={
                "channel": "goal_activity_backfill",
                "evidence_ref": message.evidence_ref,
            },
            status_callback=capture_status,
        )
        status = str(call_status.get("status") or "unknown")
        if status != "ok":
            undetermined_refs.append(message.evidence_ref)
            emit(f"  未判定（模型调用状态：{status}）")
            continue

        message_activities = _select_activities(
            candidates.get("activities", []),
            message=message,
            goals_by_id=goals_by_id,
        )
        if not message_activities:
            emit("  无动态")
            continue
        selected.extend(message_activities)
        for activity in message_activities:
            emit(
                f"  {activity.kind} → {activity.goal_title} "
                f"[{activity.goal_id}] | confidence={activity.confidence:.2f}"
            )
            emit(f"  引文：“{activity.evidence_span}”")

    write_attempts = 0
    if apply and not undetermined_refs:
        with db.transaction() as conn:
            for activity in selected:
                goal_activity_service.record(
                    activity.goal_id,
                    activity.kind,
                    "auto",
                    activity.evidence_ref,
                    evidence_span=activity.evidence_span,
                    confidence=activity.confidence,
                    created_at=activity.created_at,
                    conn=conn,
                )
        write_attempts = len(selected)

    kind_counts = Counter(activity.kind for activity in selected)
    emit("")
    emit("=== 汇总 ===")
    emit(
        f"扫描：{len(messages)}（posts={scanned_posts}，"
        f"user comments={scanned_comments}）"
    )
    emit(f"产出动态：{len(selected)}")
    emit(f"未判定：{len(undetermined_refs)}")
    emit(
        "kind 分布："
        + "，".join(
            f"{kind}={kind_counts.get(kind, 0)}" for kind in BACKFILL_ACTIVITY_KINDS
        )
    )
    if not apply:
        emit("写入：0（dry-run）")
    elif undetermined_refs:
        # 触发场景：任一帖子或评论得到 api_error/invalid_response；
        # 此时写入已判定部分会留下半份迁移，故整批保持零写入。
        emit("写入：0（存在未判定项，整批未写入）")
    else:
        emit(f"写入处理：{write_attempts}（幂等写入，既有记录与撤销墓碑保持原状）")

    return BackfillResult(
        scanned_posts=scanned_posts,
        scanned_comments=scanned_comments,
        activities=tuple(selected),
        undetermined_refs=tuple(undetermined_refs),
        write_attempts=write_attempts,
    )


def _select_activities(
    candidates: list[dict],
    *,
    message: HistoricalMessage,
    goals_by_id: dict[str, dict],
) -> list[BackfillActivity]:
    selected: list[BackfillActivity] = []
    for candidate in candidates:
        confidence = float(candidate["confidence"])
        goal_id = str(candidate["goal_id"])
        kind = str(candidate["kind"])
        evidence_span = str(candidate["evidence_span"]).strip()
        if confidence < BACKFILL_CONFIDENCE_THRESHOLD:
            continue
        if goal_id not in goals_by_id:
            continue
        if kind not in BACKFILL_ACTIVITY_KINDS:
            continue
        if not suggestion_router._span_is_verbatim(evidence_span, message.content):
            continue
        selected.append(
            BackfillActivity(
                evidence_ref=message.evidence_ref,
                source_type=message.source_type,
                created_at=message.created_at,
                goal_id=goal_id,
                goal_title=str(goals_by_id[goal_id]["title"]),
                kind=kind,
                evidence_span=evidence_span,
                confidence=confidence,
            )
        )
    return selected


def _date_text(created_at: float) -> str:
    return datetime.fromtimestamp(created_at, tz=SYSTEM_TIMEZONE).date().isoformat()


def _content_preview(content: str) -> str:
    compact = " ".join(content.split())
    if len(compact) <= CONTENT_PREVIEW_LENGTH:
        return compact
    return compact[:CONTENT_PREVIEW_LENGTH] + "…"


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not db.DB_PATH.is_file():
        raise SystemExit(f"工作区数据库不存在：{db.DB_PATH}")

    config = load_config()
    logging_service.init_logging(config.get("logging"))
    client = OpenAI(
        api_key=config["api_key"],
        base_url=config.get("base_url", "https://api.openai.com/v1"),
    )
    secondary_model.install_from_config(
        config,
        main_client=client,
        client_factory=lambda api_key, base_url: OpenAI(
            api_key=api_key,
            base_url=base_url,
        ),
    )
    result = run_backfill(client, config["model"], apply=args.apply)
    if result.undetermined_refs:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
