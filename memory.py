"""
TraceLog - Memory Layer
"""

import json
import os
from datetime import date

PROFILE_FILE = "profile.json"
DIARY_LIMIT = 50


def _empty_profile() -> dict:
    today = date.today().isoformat()
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
        "emotions": [],
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


def _upsert(existing: list, incoming: list, key: str) -> list:
    index = {item.get(key, "").strip().lower(): i for i, item in enumerate(existing)}
    for new_item in incoming:
        k = new_item.get(key, "").strip().lower()
        if k and k in index:
            existing[index[k]].update(new_item)
        else:
            existing.append(new_item)
    return existing


def merge_profile(profile: dict, extracted: dict) -> dict:
    today = date.today().isoformat()

    for field, key in [
        ("skills",  "name"),
        ("hobbies", "name"),
        ("goals",   "goal"),
        ("people",  "name"),
        ("places",  "name"),
        ("media",   "title"),
        ("todos",   "task"),
    ]:
        if field in extracted:
            profile[field] = _upsert(profile.get(field, []), extracted[field], key)

    for field in ("food", "health", "ideas", "purchases", "emotions"):
        if field in extracted:
            profile.setdefault(field, []).extend(extracted[field])

    profile.setdefault("diary_summaries", []).append({
        "date": today,
        "mood": extracted.get("mood", ""),
        "summary": extracted.get("summary", ""),
    })
    profile["diary_summaries"] = profile["diary_summaries"][-DIARY_LIMIT:]

    profile["meta"]["last_updated"] = today
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
            f"{e['date']} [{e.get('mood', '')}] {e.get('summary', '')}" for e in recent
        ))

    return "\n".join(lines)
