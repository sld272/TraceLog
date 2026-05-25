"""Profile patch service for workspace/user.md."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

import memory
from core import db

ANCHOR_RE = re.compile(r"<!--\s*id:\s*([A-Za-z0-9_-]+)\s*-->")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")

SECTION_PREFIXES = {
    "基本信息": "bf",
    "关键身份": "ki",
    "身份与现状": "status",
    "技能与专长": "sk",
    "兴趣与习惯": "hb",
    "关注的核心人际关系": "rel",
    "性格与情绪倾向": "tr",
    "长期目标与当前痛点": "gl",
}

THRESHOLDS = {
    ("normal", "add"): (1, 0.60),
    ("normal", "update"): (1, 0.60),
    ("normal", "remove"): (1, 0.85),
    ("high", "add"): (2, 0.80),
    ("high", "update"): (2, 0.80),
    ("high", "remove"): (2, 0.90),
}


@dataclass(frozen=True)
class PatchResult:
    status: str
    reason: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason}


def apply_patch(patch: dict, source: str = "reflector") -> dict:
    """Apply one user.md patch or store it for review."""
    parsed = _normalize_patch(patch)
    if parsed is None:
        return PatchResult("skipped", "invalid_patch").to_dict()

    reason = _validate_patch_gate(parsed)
    if reason is not None:
        return PatchResult("skipped", reason).to_dict()

    text = memory.read_profile()
    doc = _parse_user_md(text)
    section = parsed["section"]
    sensitivity = doc.sensitivity.get(section, "normal")

    if sensitivity == "high":
        _insert_pending_change(parsed)
        return PatchResult("pending").to_dict()

    updated = _apply_ops_to_doc(doc, parsed)
    if updated is None:
        return PatchResult("skipped", "invalid_anchor").to_dict()

    _write_user_md_revision(updated, parsed, source)
    return PatchResult("applied").to_dict()


def list_pending_changes() -> list[dict]:
    rows = db.query_all(
        """
        SELECT id, section, patch, evidence, confidence, status, created_at, resolved_at
        FROM pending_user_md_changes
        ORDER BY created_at DESC, id DESC
        """
    )
    return [
        {
            "id": row["id"],
            "section": row["section"],
            "patch": _loads_json(row["patch"], {}),
            "evidence": _loads_json(row["evidence"], []),
            "confidence": row["confidence"],
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }
        for row in rows
    ]


@dataclass
class UserMdDoc:
    lines: list[str]
    sensitivity: dict[str, str]


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
            if not isinstance(value, str) or not value.strip():
                return None
            item["value"] = value.strip()
        if kind in ("update", "remove"):
            anchor = op.get("anchor")
            if not isinstance(anchor, str) or not anchor.strip():
                return None
            item["anchor"] = anchor.strip()
        normalized_ops.append(item)

    try:
        confidence = float(patch.get("confidence"))
    except (TypeError, ValueError):
        return None

    normalized_evidence = []
    for item in evidence:
        if not isinstance(item, str) or not item.strip():
            return None
        post_id = item.strip()
        if post_id not in normalized_evidence:
            normalized_evidence.append(post_id)

    return {
        "section": section.strip(),
        "ops": normalized_ops,
        "evidence": normalized_evidence,
        "confidence": max(0.0, min(1.0, confidence)),
    }


def _validate_patch_gate(patch: dict) -> str | None:
    if not _evidence_exists(patch["evidence"]):
        return "invalid_evidence"

    text = memory.read_profile()
    doc = _parse_user_md(text)
    sensitivity = doc.sensitivity.get(patch["section"], "normal")
    for op in patch["ops"]:
        min_evidence, min_confidence = THRESHOLDS.get(
            (sensitivity, op["op"]),
            THRESHOLDS[("normal", op["op"])],
        )
        if len(patch["evidence"]) < min_evidence:
            return "insufficient_evidence"
        if patch["confidence"] < min_confidence:
            return "low_confidence"
    if _find_section_bounds(doc.lines, patch["section"]) is None:
        return "missing_section"
    bounds = _find_section_bounds(doc.lines, patch["section"])
    assert bounds is not None
    start, end = bounds
    for op in patch["ops"]:
        if op["op"] in ("update", "remove") and _find_anchor_line(doc.lines, start, end, op["anchor"]) is None:
            return "invalid_anchor"
    return None


def _evidence_exists(evidence: list[str]) -> bool:
    if not evidence:
        return False
    placeholders = ",".join("?" for _ in evidence)
    row = db.query_one(
        f"SELECT COUNT(*) AS count FROM posts WHERE id IN ({placeholders})",
        tuple(evidence),
    )
    return row is not None and row["count"] == len(set(evidence))


def _parse_user_md(text: str) -> UserMdDoc:
    lines = text.splitlines()
    sensitivity: dict[str, str] = {}
    if len(lines) >= 2 and lines[0].strip() == "---":
        try:
            end = lines.index("---", 1)
        except ValueError:
            end = -1
        if end > 0:
            in_sensitivity = False
            for line in lines[1:end]:
                stripped = line.strip()
                if stripped == "sensitivity:":
                    in_sensitivity = True
                    continue
                if in_sensitivity:
                    if not line.startswith("  ") or ":" not in stripped:
                        if stripped:
                            in_sensitivity = False
                        continue
                    key, value = stripped.split(":", 1)
                    level = value.strip()
                    if level in ("high", "normal"):
                        sensitivity[key.strip()] = level
    return UserMdDoc(lines=lines, sensitivity=sensitivity)


def _apply_ops_to_doc(doc: UserMdDoc, patch: dict) -> str | None:
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
    prefix = SECTION_PREFIXES.get(section, "sec")
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _write_user_md_revision(content: str, patch: dict, source: str) -> None:
    _write_text_atomic(memory.USER_MD_PATH, content)
    db.execute(
        """
        INSERT INTO user_md_revisions(snapshot, patch, source, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (content, json.dumps(patch, ensure_ascii=False), source, db.now_ts()),
    )


def _insert_pending_change(patch: dict) -> None:
    db.execute(
        """
        INSERT INTO pending_user_md_changes(section, patch, evidence, confidence, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            patch["section"],
            json.dumps(patch, ensure_ascii=False),
            json.dumps(patch["evidence"], ensure_ascii=False),
            patch["confidence"],
            db.now_ts(),
        ),
    )


def _write_text_atomic(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _loads_json(value: str | None, default):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return default
