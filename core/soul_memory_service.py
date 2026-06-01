"""SOUL-specific memory file service."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core import db

SOUL_MEMORIES_DIR = db.WORKSPACE_DIR / "soul_memories"
ANCHOR_RE = re.compile(r"<!--\s*id:\s*([A-Za-z0-9_-]+)\s*-->")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
SECTION_PREFIXES = {
    "对用户的理解": "understand",
    "我们之间的互动约定": "rule",
    "私聊沉淀": "chat",
}


def default_soul_memory(name: str) -> str:
    now = datetime.now().astimezone().isoformat()
    return f"""---
schema: tracelog/soul_memory.md@v1
soul: {name}
updated_at: {now}
---

# {name}的相处记忆

## 对用户的理解

## 我们之间的互动约定

## 私聊沉淀
"""


def soul_memory_path(name: str) -> Path:
    _validate_memory_name(name)
    return SOUL_MEMORIES_DIR / f"{name}.md"


def read_soul_memory(name: str) -> str:
    path = soul_memory_path(name)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_soul_memory(
    name: str,
    content: str,
    source: str = "user",
    patch: dict | None = None,
) -> None:
    """Overwrite one SOUL memory file and record a revision."""
    _ensure_soul_exists(name)
    if not isinstance(content, str):
        raise ValueError("SOUL 记忆内容必须是字符串")

    SOUL_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(soul_memory_path(name), content)
    db.execute(
        """
        INSERT INTO soul_memory_revisions(soul_name, snapshot, patch, source, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            name,
            content,
            json.dumps(patch or {"op": "overwrite_soul_memory"}, ensure_ascii=False),
            source,
            db.now_ts(),
        ),
    )


def apply_patch(name: str, patch: dict, source: str = "soul_deep_reflector") -> dict:
    """Apply one SOUL memory patch when it references valid sections and anchors."""
    _ensure_soul_exists(name)
    parsed = _normalize_patch(patch)
    if parsed is None:
        return {"status": "skipped", "reason": "invalid_patch"}

    reason = _validate_patch(name, parsed)
    if reason is not None:
        return {"status": "skipped", "reason": reason}

    text = read_soul_memory(name)
    doc = _parse_soul_memory(text)
    updated = _apply_ops_to_doc(doc, parsed)
    if updated is None:
        return {"status": "skipped", "reason": "invalid_anchor"}

    write_soul_memory(name, updated, source=source, patch=parsed)
    return {"status": "applied", "reason": None}


@dataclass
class SoulMemoryDoc:
    lines: list[str]


def _normalize_patch(patch: dict) -> dict | None:
    if not isinstance(patch, dict):
        return None
    section = patch.get("section")
    ops = patch.get("ops")
    evidence = patch.get("evidence")
    if not isinstance(section, str) or not section.strip():
        return None
    if not isinstance(ops, list) or not ops:
        return None
    if not isinstance(evidence, list):
        return None

    normalized_ops = []
    for op in ops:
        if not isinstance(op, dict):
            return None
        kind = op.get("op")
        if kind not in ("add", "update", "remove"):
            return None
        item: dict[str, Any] = {"op": kind}
        if kind in ("add", "update"):
            value = op.get("value")
            if not isinstance(value, str):
                return None
            item["value"] = value.strip()
        if kind in ("update", "remove"):
            anchor = op.get("anchor")
            if not isinstance(anchor, str) or not anchor.strip():
                return None
            item["anchor"] = anchor.strip()
        normalized_ops.append(item)

    raw_confidence = patch.get("confidence")
    if not isinstance(raw_confidence, (int, float, str)):
        return None
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        return None

    normalized_evidence = []
    for item in evidence:
        if not isinstance(item, str) or not item.strip():
            return None
        evidence_id = item.strip()
        if evidence_id not in normalized_evidence:
            normalized_evidence.append(evidence_id)

    return {
        "section": section.strip(),
        "ops": normalized_ops,
        "evidence": normalized_evidence,
        "confidence": max(0.0, min(1.0, confidence)),
    }


def _validate_patch(name: str, patch: dict) -> str | None:
    if patch["confidence"] < 0.65:
        return "low_confidence"
    if not _evidence_exists(name, patch["evidence"]):
        return "invalid_evidence"

    text = read_soul_memory(name)
    doc = _parse_soul_memory(text)
    bounds = _find_section_bounds(doc.lines, patch["section"])
    if bounds is None:
        return "missing_section"
    start, end = bounds
    for op in patch["ops"]:
        if op["op"] in ("add", "update") and not _is_meaningful_value(op["value"]):
            return "invalid_value"
        if op["op"] in ("update", "remove") and _find_anchor_line(doc.lines, start, end, op["anchor"]) is None:
            return "invalid_anchor"
    return None


def _evidence_exists(name: str, evidence: list[str]) -> bool:
    if not evidence:
        return False
    for item in evidence:
        kind, sep, raw_id = item.partition(":")
        if not sep or not raw_id:
            return False
        if kind == "post":
            row = db.query_one(
                """
                SELECT 1
                FROM posts
                JOIN comments ON comments.post_id = posts.id
                WHERE posts.id = ? AND comments.soul_name = ?
                """,
                (raw_id, name),
            )
        elif kind == "comment":
            try:
                comment_id = int(raw_id)
            except ValueError:
                return False
            row = db.query_one(
                "SELECT 1 FROM comments WHERE id = ? AND soul_name = ?",
                (comment_id, name),
            )
        elif kind == "chat_message":
            try:
                message_id = int(raw_id)
            except ValueError:
                return False
            row = db.query_one(
                """
                SELECT 1
                FROM chat_messages
                JOIN chat_threads ON chat_threads.id = chat_messages.thread_id
                WHERE chat_messages.id = ? AND chat_threads.soul_name = ?
                """,
                (message_id, name),
            )
        elif kind == "comment_message":
            try:
                message_id = int(raw_id)
            except ValueError:
                return False
            row = db.query_one(
                "SELECT 1 FROM comments WHERE id = ? AND soul_name = ? AND seq > 0",
                (message_id, name),
            )
        else:
            return False
        if row is None:
            return False
    return True


def _is_meaningful_value(value: str) -> bool:
    normalized = re.sub(r"[\s\-_*`~。．.，,；;：:（）()【】\[\]]+", "", value)
    return normalized not in {"", "暂无", "待补充", "未知", "无", "没有", "不详", "暂无可补充", "暂未补充"}


def _parse_soul_memory(text: str) -> SoulMemoryDoc:
    return SoulMemoryDoc(lines=text.splitlines())


def _apply_ops_to_doc(doc: SoulMemoryDoc, patch: dict) -> str | None:
    lines = list(doc.lines)
    section = patch["section"]
    for op in patch["ops"]:
        bounds = _find_section_bounds(lines, section)
        if bounds is None:
            return None
        start, end = bounds
        if op["op"] == "add":
            anchor = _new_anchor(section)
            insert_at = _section_insert_index(lines, start, end)
            lines.insert(insert_at, f"- {op['value']} <!-- id: {anchor} -->")
        elif op["op"] == "update":
            index = _find_anchor_line(lines, start, end, op["anchor"])
            if index is None:
                return None
            lines[index] = f"- {op['value']} <!-- id: {op['anchor']} -->"
        elif op["op"] == "remove":
            index = _find_anchor_line(lines, start, end, op["anchor"])
            if index is None:
                return None
            del lines[index]
    return "\n".join(lines).rstrip() + "\n"


def _find_section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    start = None
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if match and match.group(1).strip() == section:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if SECTION_RE.match(lines[index]):
            end = index
            break
    return start, end


def _section_insert_index(lines: list[str], start: int, end: int) -> int:
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    return insert_at


def _find_anchor_line(lines: list[str], start: int, end: int, anchor: str) -> int | None:
    for index in range(start + 1, end):
        match = ANCHOR_RE.search(lines[index])
        if match and match.group(1) == anchor:
            return index
    return None


def _new_anchor(section: str) -> str:
    prefix = SECTION_PREFIXES.get(section, "sm")
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _ensure_soul_exists(name: str) -> None:
    _validate_memory_name(name)
    row = db.query_one("SELECT 1 FROM souls WHERE name = ?", (name,))
    if row is None:
        raise ValueError(f"SOUL 不存在：{name}")


def _validate_memory_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("SOUL 名称不能为空")
    if name != name.strip():
        raise ValueError("SOUL 名称不能包含首尾空白")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("SOUL 名称不能包含路径分隔符或路径穿越")
    if any(ord(char) < 32 for char in name):
        raise ValueError("SOUL 名称不能包含控制字符")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
