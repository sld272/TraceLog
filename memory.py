"""
TraceLog - Memory Layer
"""

import json
import os
from datetime import date, datetime, timezone, timedelta

BEIJING = timezone(timedelta(hours=8))

PROFILE_FILE = "profile.json"
DIARY_LIMIT = 50


def _empty_profile() -> dict:
    today = datetime.now(BEIJING).date().isoformat()
    return {
        "meta": {"created_at": today, "last_updated": today, "entry_count": 0},
        "portrait": "",
        "skills": [],
        "hobbies": [],
        "todos": [],
        "goals": [],
        "people": [],
        "places": [],
        "media": [],
        "food": [],
        "health": [],
        "ideas": [],
        "purchases": [],
        "diary_summaries": [],
    }


def load_profile() -> dict:
    if os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return _empty_profile()


def save_profile(profile: dict) -> None:
    tmp = PROFILE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROFILE_FILE)


def apply_profile_update(profile: dict, updated: dict, raw_input: str = "") -> dict:
    now = datetime.now(BEIJING)
    today = now.date().isoformat()
    timestamp = now.strftime("%Y-%m-%dT%H:%M")

    for field in ("skills", "hobbies", "todos", "goals", "people", "places",
                  "media", "food", "health", "ideas", "purchases"):
        if field in updated:
            profile[field] = updated[field]

    profile.setdefault("diary_summaries", []).append({
        "timestamp": timestamp,
        "mood": updated.get("mood", ""),
        "summary": updated.get("summary", ""),
        "content": raw_input,
    })
    profile["diary_summaries"] = profile["diary_summaries"][-DIARY_LIMIT:]

    profile["meta"]["last_updated"] = timestamp
    profile["meta"]["entry_count"] = profile["meta"].get("entry_count", 0) + 1

    return profile


def build_context_summary(profile: dict) -> str:
    if profile["meta"].get("entry_count", 0) == 0:
        return ""

    lines = []

    if profile.get("portrait"):
        lines.append(f"【关于你】\n{profile['portrait']}")

    def join_items(items, fmt):
        parts = [s for s in (fmt(i) for i in items) if s]
        return "、".join(parts) if parts else None

    skills = join_items(
        profile.get("skills", []),
        lambda i: (f"{i['name']}（{i['proficiency']}）" if i.get("proficiency") else i["name"]) if i.get("name") else None,
    )
    if skills:
        lines.append(f"技能：{skills}")

    hobbies = join_items(profile.get("hobbies", []), lambda i: i.get("name"))
    if hobbies:
        lines.append(f"兴趣：{hobbies}")

    goals = join_items(
        profile.get("goals", []),
        lambda i: f"{i['goal']}（{i.get('status', '')}）" if i.get("goal") else None,
    )
    if goals:
        lines.append(f"目标：{goals}")

    pending = [t for t in profile.get("todos", []) if t.get("status") != "已完成"][-8:]
    if pending:
        lines.append("近期待办：" + "、".join(
            f"{t['task']}（{t.get('date') or '待定'}）" for t in pending
        ))

    people = join_items(
        profile.get("people", []),
        lambda i: f"{i['name']}（{i.get('relation', '')}）" if i.get("name") else None,
    )
    if people:
        lines.append(f"身边的人：{people}")

    places = join_items(
        profile.get("places", []),
        lambda i: f"{i['name']}（{i.get('type', '')}）" if i.get("name") else None,
    )
    if places:
        lines.append(f"常去地点：{places}")

    recent = profile.get("diary_summaries", [])[-5:]
    if recent:
        lines.append("近期日记：\n  " + "\n  ".join(
            f"{e['timestamp']} [{e.get('mood', '')}] {e.get('summary', '')}" for e in recent
        ))

    return "\n".join(lines)
