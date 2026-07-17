"""Shared schedule/goal prompt sections for every reply channel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from core import db, goal_schedule_service
from core.schedule_service import ScheduleService

LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
RECENT_PAST_DAYS = 2
RECENT_FUTURE_DAYS = 7
RECENT_SCHEDULE_LIMIT = 20
MENTIONED_SCHEDULE_LIMIT = 5

_GOAL_STATUS_LABELS = {
    "done": "已完成",
    "abandoned": "已放弃",
    "paused": "已暂停",
}
_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@dataclass(frozen=True)
class RecentScheduleContext:
    section: str
    event_ids: frozenset[str]


@dataclass(frozen=True)
class _RenderableEvent:
    raw: Mapping[str, Any]
    event_id: str
    start: datetime
    end: datetime
    all_day: bool


def build_recent_schedule_context(
    context_date: date | None = None,
    *,
    now: datetime | None = None,
    service: ScheduleService | None = None,
    limit: int = RECENT_SCHEDULE_LIMIT,
) -> RecentScheduleContext:
    """Build the always-on recent schedule section and its displayed event ids."""
    # 没有任何日历账号（用户从未启用日程功能）时不渲染区块，避免模型把
    # "日历为空" 误当成 "用户没有安排"；有账号但窗口为空时保留显式空态，
    # 让模型能确定地回答"没安排"。
    if not _has_calendar_accounts():
        return RecentScheduleContext(section="", event_ids=frozenset())
    reference = _reference_now(context_date=context_date, now=now)
    current_date = reference.date()
    start_date = current_date - timedelta(days=RECENT_PAST_DAYS)
    end_date = current_date + timedelta(days=RECENT_FUTURE_DAYS)
    if service is None:
        raw_events = _recent_event_rows(start_date, end_date)
    else:
        raw_events = service.list_events(start_date, end_date).get("events") or []
    events = [
        parsed
        for event in raw_events
        if (parsed := _parse_event(event)) is not None
    ]

    grouped: dict[str, list[_RenderableEvent]] = {
        "今天": [],
        "未来": [],
        "已结束": [],
    }
    for event in events:
        grouped[_event_group(event, reference)].append(event)
    grouped["今天"].sort(key=lambda event: (event.start, event.end, event.event_id))
    grouped["未来"].sort(key=lambda event: (event.start, event.end, event.event_id))
    grouped["已结束"].sort(
        key=lambda event: (event.end, event.start, event.event_id), reverse=True
    )

    ordered = grouped["今天"] + grouped["未来"] + grouped["已结束"]
    selected = ordered[: max(0, limit)]
    selected_ids = frozenset(event.event_id for event in selected if event.event_id)
    selected_by_group = {
        name: [event for event in grouped[name] if event in selected]
        for name in ("今天", "未来", "已结束")
    }

    lines = [
        "# 近期日程",
        "",
        f"[过去 {RECENT_PAST_DAYS} 天至未来 {RECENT_FUTURE_DAYS} 天]",
        f"本周共 {_weekly_density(reference)} 项安排",
    ]
    progress_by_goal: dict[str, dict[str, Any]] = {}
    if selected:
        for group_name in ("今天", "未来", "已结束"):
            group_events = selected_by_group[group_name]
            if not group_events:
                continue
            lines.extend(["", f"## {group_name}"])
            lines.extend(
                _render_recent_event(event, reference, progress_by_goal)
                for event in group_events
            )
    else:
        lines.extend(["", "（窗口内暂无安排）"])

    truncated = len(ordered) - len(selected)
    if truncated > 0:
        lines.extend(["", f"（另有 {truncated} 条未列出）"])
    return RecentScheduleContext(section="\n".join(lines), event_ids=selected_ids)


def build_mentioned_schedule_section(
    keywords: Sequence[str] | None,
    *,
    exclude_event_ids: Iterable[str] = (),
    context_date: date | None = None,
    now: datetime | None = None,
    limit: int = MENTIONED_SCHEDULE_LIMIT,
) -> str:
    """Build keyword-hit schedule/old-goal anchors from the full local cache."""
    normalized_keywords = _normalize_keywords(keywords)
    if not normalized_keywords or limit <= 0:
        return ""

    reference = _reference_now(context_date=context_date, now=now)
    excluded = {str(event_id) for event_id in exclude_event_ids if str(event_id)}
    event_rows = _matching_event_rows(normalized_keywords, excluded)
    goal_rows = _matching_goal_rows(normalized_keywords)

    candidates: list[tuple[int, int, float, str, str]] = []
    for row in event_rows:
        event_data = dict(row)
        event = _parse_event(event_data)
        if event is None:
            continue
        match_rank = _match_rank(
            (str(event_data["subject"] or ""), str(event_data["location"] or "")),
            normalized_keywords,
        )
        distance = abs(event.start.timestamp() - reference.timestamp())
        line = _render_mentioned_event(event, reference)
        candidates.append((match_rank, 0, distance, event.event_id, line))

    for row in goal_rows:
        title = str(row["title"] or "").strip()
        if not title:
            continue
        match_rank = _match_rank((title,), normalized_keywords)
        updated_at = float(row["updated_at"] or 0.0)
        status = _GOAL_STATUS_LABELS.get(str(row["status"]), str(row["status"]))
        month = datetime.fromtimestamp(updated_at, LOCAL_TIMEZONE).strftime("%Y-%m")
        line = f"- 曾有目标：{title}（{status}，{month}）"
        candidates.append((match_rank, 1, -updated_at, str(row["id"]), line))

    candidates.sort(key=lambda item: item[:4])
    rendered = [candidate[4] for candidate in candidates[:limit]]
    if not rendered:
        return ""
    return "# 提及的日程\n\n" + "\n".join(rendered)


def _has_calendar_accounts() -> bool:
    return db.query_one("SELECT 1 FROM calendar_accounts LIMIT 1") is not None


def _matching_event_rows(keywords: Sequence[str], excluded: set[str]) -> list[Any]:
    matches = " OR ".join(
        "(COALESCE(subject, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
        "OR COALESCE(location, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        for _ in keywords
    )
    params: list[Any] = []
    for keyword in keywords:
        pattern = _like_pattern(keyword)
        params.extend((pattern, pattern))
    exclusion = ""
    if excluded:
        placeholders = ", ".join("?" for _ in excluded)
        exclusion = f" AND id NOT IN ({placeholders})"
        params.extend(sorted(excluded))
    return db.query_all(
        f"""
        SELECT id, subject, start_ts, end_ts, start_local, end_local, all_day, location
        FROM schedule_events
        WHERE is_cancelled = 0
          AND ({matches})
          {exclusion}
        ORDER BY start_ts, end_ts, id
        """,
        tuple(params),
    )


def _recent_event_rows(start_date: date, end_date: date) -> list[dict[str, Any]]:
    start = datetime.combine(start_date, time.min, LOCAL_TIMEZONE)
    exclusive_end = datetime.combine(end_date + timedelta(days=1), time.min, LOCAL_TIMEZONE)
    rows = db.query_all(
        """
        SELECT id, subject, start_ts, end_ts, start_local, end_local, all_day, location
        FROM schedule_events
        WHERE is_cancelled = 0
          AND end_ts > ?
          AND start_ts < ?
        ORDER BY start_ts, end_ts, id
        """,
        (start.timestamp(), exclusive_end.timestamp()),
    )
    events = [dict(row) for row in rows]
    links = goal_schedule_service.links_for_events([str(event["id"]) for event in events])
    for event in events:
        event["goal_links"] = links.get(str(event["id"]), [])
    return events


def _matching_goal_rows(keywords: Sequence[str]) -> list[Any]:
    matches = " OR ".join(
        "title LIKE ? ESCAPE '\\' COLLATE NOCASE" for _ in keywords
    )
    params = tuple(_like_pattern(keyword) for keyword in keywords)
    return db.query_all(
        f"""
        SELECT id, title, status, updated_at
        FROM goals
        WHERE status != 'active'
          AND ({matches})
        ORDER BY updated_at DESC, id
        """,
        params,
    )


def _render_recent_event(
    event: _RenderableEvent,
    reference: datetime,
    progress_by_goal: dict[str, dict[str, Any]],
) -> str:
    when = _recent_time_label(event, reference)
    subject = str(event.raw.get("subject") or "（无标题）").strip() or "（无标题）"
    location = _location_suffix(event.raw)
    goals = _goal_progress_suffix(event.raw, reference, progress_by_goal)
    return f"- {when} {subject}{location}{goals}"


def _render_mentioned_event(event: _RenderableEvent, reference: datetime) -> str:
    distance = _event_distance_label(event, reference)
    when = _absolute_time_label(event)
    subject = str(event.raw.get("subject") or "（无标题）").strip() or "（无标题）"
    return f"- [{distance}] {when} {subject}{_location_suffix(event.raw)}"


def _goal_progress_suffix(
    event: Mapping[str, Any],
    reference: datetime,
    progress_by_goal: dict[str, dict[str, Any]],
) -> str:
    goal_details: list[str] = []
    for goal_link in event.get("goal_links") or []:
        goal_id = str(goal_link.get("goal_id") or "")
        goal_title = str(goal_link.get("goal_title") or "").strip()
        if not goal_id or not goal_title:
            continue
        progress = progress_by_goal.get(goal_id)
        if progress is None:
            try:
                progress = goal_schedule_service.weekly_progress(goal_id, now=reference)
            except goal_schedule_service.GoalNotFoundError:
                continue
            progress_by_goal[goal_id] = progress
        expectation = progress.get("expectation")
        if expectation and progress.get("text"):
            goal_details.append(
                f"目标：{goal_title}（{expectation['label']}，本周 {progress['text']}）"
            )
        else:
            goal_details.append(f"目标：{goal_title}")
    return f"；{'；'.join(goal_details)}" if goal_details else ""


def _weekly_density(reference: datetime) -> int:
    monday = reference.date() - timedelta(days=reference.weekday())
    week_start = datetime.combine(monday, time.min, LOCAL_TIMEZONE)
    week_end = week_start + timedelta(days=7)
    row = db.query_one(
        """
        SELECT COUNT(*) AS event_count
        FROM schedule_events
        WHERE is_cancelled = 0
          AND end_ts > ?
          AND start_ts < ?
        """,
        (week_start.timestamp(), week_end.timestamp()),
    )
    return int(row["event_count"]) if row is not None else 0


def _parse_event(event: Mapping[str, Any]) -> _RenderableEvent | None:
    try:
        start = _local_datetime(event["start_local"])
        end = _local_datetime(event["end_local"])
    except (KeyError, TypeError, ValueError):
        return None
    return _RenderableEvent(
        raw=event,
        event_id=str(event.get("id") or ""),
        start=start,
        end=end,
        all_day=bool(event.get("all_day")),
    )


def _event_group(event: _RenderableEvent, reference: datetime) -> str:
    if event.end <= reference:
        return "已结束"
    if event.start.date() <= reference.date():
        return "今天"
    return "未来"


def _recent_time_label(event: _RenderableEvent, reference: datetime) -> str:
    day = _relative_day_label(event.start.date(), reference.date())
    if event.end <= reference:
        day += "·已结束"
    if event.all_day:
        return f"{day} 全天"
    if event.end.date() == event.start.date():
        return f"{day} {event.start:%H:%M}–{event.end:%H:%M}"
    end_day = _relative_day_label(event.end.date(), reference.date())
    return f"{day} {event.start:%H:%M}–{end_day} {event.end:%H:%M}"


def _relative_day_label(target: date, current: date) -> str:
    delta = (target - current).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return f"明天（{_WEEKDAY_LABELS[target.weekday()]}）"
    if delta == -1:
        return "昨天"
    if delta == -2:
        return "前天"
    if delta > 1:
        return f"{delta} 天后（{_WEEKDAY_LABELS[target.weekday()]}）"
    return f"{-delta} 天前"


def _event_distance_label(event: _RenderableEvent, reference: datetime) -> str:
    if event.end <= reference:
        days = max(0, (reference.date() - event.end.date()).days)
        if days == 0:
            return "已过去不到 1 天"
        if days < 14:
            return f"已过去 {days} 天"
        return f"已过去 {days // 7} 周"
    if event.start > reference:
        days = max(0, (event.start.date() - reference.date()).days)
        if days == 0:
            return "今天稍后"
        if days < 14:
            return f"{days} 天后"
        return f"{days // 7} 周后"
    return "正在进行"


def _absolute_time_label(event: _RenderableEvent) -> str:
    if event.all_day:
        return f"{event.start.date().isoformat()} 全天"
    if event.end.date() == event.start.date():
        return f"{event.start.date().isoformat()} {event.start:%H:%M}–{event.end:%H:%M}"
    return (
        f"{event.start.date().isoformat()} {event.start:%H:%M}–"
        f"{event.end.date().isoformat()} {event.end:%H:%M}"
    )


def _location_suffix(event: Mapping[str, Any]) -> str:
    location = str(event.get("location") or "").strip()
    return f"；地点：{location}" if location else ""


def _local_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def _reference_now(
    *,
    context_date: date | None = None,
    now: datetime | None = None,
) -> datetime:
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=LOCAL_TIMEZONE)
        return now.astimezone(LOCAL_TIMEZONE)
    if context_date is not None:
        return datetime.combine(context_date, time(hour=12), LOCAL_TIMEZONE)
    return datetime.now(LOCAL_TIMEZONE)


def _normalize_keywords(keywords: Sequence[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in keywords or []:
        keyword = str(value or "").strip()
        folded = keyword.casefold()
        if len(keyword) < 2 or folded in seen:
            continue
        seen.add(folded)
        normalized.append(keyword)
    return normalized


def _like_pattern(keyword: str) -> str:
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _match_rank(texts: Sequence[str], keywords: Sequence[str]) -> int:
    folded_text = "\n".join(texts).casefold()
    for index, keyword in enumerate(keywords):
        if keyword.casefold() in folded_text:
            return index
    return len(keywords)
