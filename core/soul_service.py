"""Read-only SOUL loading helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core import db


@dataclass(frozen=True)
class SoulContext:
    name: str
    description: str | None
    sort_order: int
    persona: str
    soul_memory: str


def list_enabled_souls() -> list[SoulContext]:
    """Load enabled SOUL persona and memory files in display order."""
    rows = db.query_all(
        """
        SELECT name, file_path, sort_order, description
        FROM souls
        WHERE enabled = 1
        ORDER BY sort_order, name
        """
    )

    souls: list[SoulContext] = []
    for row in rows:
        persona_path = db.WORKSPACE_DIR / row["file_path"]
        persona = _read_optional_text(persona_path)
        if persona is None:
            continue

        soul_memory = _read_optional_text(
            db.WORKSPACE_DIR / "soul_memories" / f"{row['name']}.md"
        )
        souls.append(
            SoulContext(
                name=row["name"],
                description=row["description"],
                sort_order=row["sort_order"],
                persona=persona,
                soul_memory=soul_memory or "",
            )
        )
    return souls


def _read_optional_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
