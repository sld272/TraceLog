"""Deterministic Chinese relative-date normalization (day granularity).

The same phrase ("下周三要交报告") used to be date-resolved independently by
every LLM chain (todo extraction, goal extraction, memory reconcile), and an
ambiguous expression interpreted N times diverges. Worse, backlogged evidence
gets reconciled hours or days later, so "明天" resolved against the processing
clock lands on the wrong day. This module is the single place where relative
time is interpreted: consumers call it at prompt-render time with the anchor
that is correct for THEM (speech time for live extraction, the event's own
timestamp for reconcile), inject the result as an inline annotation, and the
LLM copies dates instead of computing them.

Pure rules, no LLM, no IO, no schema. Better to skip than to mislabel: past
or periodic expressions ("上周三", "每周三", "周末") are left unannotated.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class TimeAnnotation:
    """One resolved relative-time expression found in a text."""

    span: str  # the original fragment, e.g. "下周三"
    date: str  # resolved date, YYYY-MM-DD
    weekday_label: str  # e.g. "周三"
    ambiguous: bool = False
    alternative: str | None = None  # the other colloquial reading, YYYY-MM-DD


_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEKDAY_LABELS = "一二三四五六日"
_WEEK_CHARS = "一二三四五六日天"

_DAY_WORDS = {
    "今天": 0, "今早": 0, "今晚": 0, "今夜": 0,
    "明天": 1, "明早": 1, "明晚": 1,
    "后天": 2, "大后天": 3,
    "昨天": -1, "前天": -2, "大前天": -3,
}

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# One alternation scanned left-to-right; earlier branches win at the same
# position, and a match consumes its span so sub-expressions ("下周三" vs a
# bare "周三") are never annotated twice. skip_* branches deliberately consume
# expressions we refuse to annotate (past weeks, periodic phrasing, other
# months' 月底/月初/月中) so their tails cannot leak into laxer branches.
_PATTERN = re.compile(
    rf"(?P<skip_week>(?:上+|每)个?(?:周|星期|礼拜)[{_WEEK_CHARS}])"
    r"|(?P<skip_month>[上下]个?月[底初中])"
    rf"|(?P<nnext_week>下下个?(?:周|星期|礼拜)(?P<nnext_wd>[{_WEEK_CHARS}]))"
    rf"|(?P<next_week>下个?(?:周|星期|礼拜)(?P<next_wd>[{_WEEK_CHARS}]))"
    rf"|(?P<this_week>[这本]个?(?:周|星期|礼拜)(?P<this_wd>[{_WEEK_CHARS}]))"
    rf"|(?P<bare_week>(?:周|星期|礼拜)(?P<bare_wd>[{_WEEK_CHARS}]))"
    r"|(?P<day_word>大后天|大前天|(?<![以之今明史日稍往])后天|(?<![目之当以大史今明])前天|明[天早晚]|今[天早晚夜]|昨天)"
    r"|(?P<month_day>(?<![0-9年])(?P<md_month>[0-9]{1,2})月(?P<md_day>[0-9]{1,2})[日号])"
    r"|(?P<days_later>(?P<dl_num>[0-9]{1,3}|[一二两三四五六七八九十]{1,3})天之?后)"
    r"|(?P<month_part>月(?P<mp_part>[底初中]))"
)


def _anchor_date(anchor: datetime) -> date:
    """Calendar day of the anchor, localized (aware anchors -> system tz)."""
    if anchor.tzinfo is not None:
        anchor = anchor.astimezone()
    return anchor.date()


def _week_target(anchor_day: date, weekday: int, week_offset: int) -> date:
    """The given weekday of the ISO week ``week_offset`` weeks after anchor's."""
    return anchor_day + timedelta(days=weekday - anchor_day.weekday() + 7 * week_offset)


def _upcoming(anchor_day: date, weekday: int, *, include_today: bool) -> date:
    """The nearest given weekday at or after anchor day."""
    delta = (weekday - anchor_day.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return anchor_day + timedelta(days=delta)


def _upcoming_month_day(anchor_day: date, month: int, day: int) -> date | None:
    """Nearest valid occurrence of an unqualified month/day on or after anchor.

    Chinese scheduling phrases normally omit the year, so a January date spoken
    at the end of December belongs to the next year. Leap-day searches may need
    to advance more than one year; impossible dates return None."""
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    for year in range(anchor_day.year, anchor_day.year + 9):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= anchor_day:
            return candidate
    return None


def _parse_day_count(token: str) -> int | None:
    """Parse "3" / "三" / "十" / "二十一" (1..99). None when unsure."""
    if token.isdigit():
        count = int(token)
        return count if count > 0 else None
    if "十" in token:
        head, _, tail = token.partition("十")
        if (head and head not in _CN_DIGITS) or (tail and tail not in _CN_DIGITS):
            return None
        return (_CN_DIGITS[head] if head else 1) * 10 + (_CN_DIGITS[tail] if tail else 0)
    if len(token) == 1 and token in _CN_DIGITS:
        return _CN_DIGITS[token]
    return None


def _annotation(
    span: str,
    target: date,
    *,
    ambiguous: bool = False,
    alternative: str | None = None,
) -> TimeAnnotation:
    return TimeAnnotation(
        span=span,
        date=target.isoformat(),
        weekday_label=f"周{_WEEKDAY_LABELS[target.weekday()]}",
        ambiguous=ambiguous or alternative is not None,
        alternative=alternative,
    )


def extract(text: str, *, anchor: datetime) -> list[TimeAnnotation]:
    """Resolve every recognized relative-time expression in ``text`` against
    ``anchor``. Returns annotations in textual order, unresolved spans skipped."""
    if not text:
        return []
    anchor_day = _anchor_date(anchor)
    annotations: list[TimeAnnotation] = []
    for match in _PATTERN.finditer(text):
        if match.group("skip_week") or match.group("skip_month"):
            continue
        span = match.group(0)
        if match.group("nnext_week"):
            target = _week_target(anchor_day, _WEEKDAY_INDEX[match.group("nnext_wd")], 2)
            annotations.append(_annotation(span, target))
        elif match.group("next_week"):
            # Standard reading: that weekday of the NEXT ISO week. Colloquially
            # "下周X" sometimes means the upcoming X; when the two differ we
            # keep the standard one and surface the other as ``alternative``.
            weekday = _WEEKDAY_INDEX[match.group("next_wd")]
            standard = _week_target(anchor_day, weekday, 1)
            colloquial = _upcoming(anchor_day, weekday, include_today=False)
            annotations.append(_annotation(
                span,
                standard,
                alternative=colloquial.isoformat() if colloquial != standard else None,
            ))
        elif match.group("this_week"):
            target = _week_target(anchor_day, _WEEKDAY_INDEX[match.group("this_wd")], 0)
            annotations.append(_annotation(span, target))
        elif match.group("bare_week"):
            # Bare "周X" = the upcoming X; today if anchor already is that day.
            target = _upcoming(anchor_day, _WEEKDAY_INDEX[match.group("bare_wd")], include_today=True)
            annotations.append(_annotation(span, target))
        elif match.group("day_word"):
            annotations.append(_annotation(span, anchor_day + timedelta(days=_DAY_WORDS[span])))
        elif match.group("month_day"):
            target = _upcoming_month_day(
                anchor_day,
                int(match.group("md_month")),
                int(match.group("md_day")),
            )
            if target is None:
                continue
            annotations.append(_annotation(span, target))
        elif match.group("days_later"):
            count = _parse_day_count(match.group("dl_num"))
            if count is None:
                continue
            annotations.append(_annotation(span, anchor_day + timedelta(days=count)))
        elif match.group("month_part"):
            part = match.group("mp_part")
            if part == "底":
                day_of_month = calendar.monthrange(anchor_day.year, anchor_day.month)[1]
            elif part == "初":
                day_of_month = 1
            else:  # 月中
                day_of_month = 15
            annotations.append(_annotation(span, anchor_day.replace(day=day_of_month), ambiguous=True))
    return annotations


def annotation_note(text: str, *, anchor: datetime) -> str | None:
    """Format extract() results as a single annotation line, e.g.
    ``下周三＝2026-07-22（周三；口语中也可能指 2026-07-15）；月底≈2026年7月末（模糊时间，未指定具体日期）``.
    Duplicate spans collapse to their first resolution; None when nothing hits."""
    annotations = extract(text, anchor=anchor)
    if not annotations:
        return None
    seen: set[str] = set()
    parts: list[str] = []
    for item in annotations:
        if item.span in seen:
            continue
        seen.add(item.span)
        if item.alternative:
            parts.append(f"{item.span}＝{item.date}（{item.weekday_label}；口语中也可能指 {item.alternative}）")
        elif item.ambiguous:
            target = date.fromisoformat(item.date)
            part_label = {"底": "末", "初": "初", "中": "中"}.get(item.span[-1], "")
            parts.append(
                f"{item.span}≈{target.year}年{target.month}月{part_label}"
                "（模糊时间，未指定具体日期）"
            )
        else:
            parts.append(f"{item.span}＝{item.date}（{item.weekday_label}）")
    return "；".join(parts)
