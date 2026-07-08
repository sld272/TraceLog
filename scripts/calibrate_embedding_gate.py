"""Calibrate the semantic-gate floors for a (new) embedding model.

Absolute cosine bands shift between embedding models, so swapping models used
to mean re-tuning magic numbers by feel. This probe turns it into one script
run: a small fixed set of labeled query-unit pairs is embedded with the
CURRENTLY CONFIGURED embedding model, similarities are computed, and the
threshold maximizing Youden's J (TPR - FPR) is reported alongside suggested
values for memory_read.SEMANTIC_SIM_FALLBACK_FLOOR / SEMANTIC_SIM_HARD_FLOOR.
The result is persisted in the meta table keyed by the embedding config hash,
so a model you already calibrated prints its stored answer.

Probe file format (JSON, default scripts/embedding_probes.json):

    {"probes": [{"query": "...", "content": "...", "relevant": true}, ...]}

Usage:
    conda run -n tracelog python scripts/calibrate_embedding_gate.py
    conda run -n tracelog python scripts/calibrate_embedding_gate.py --probes my.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from core import db
from core.cli.config import load_config

DEFAULT_PROBES = Path(__file__).with_name("embedding_probes.json")
META_KEY_PREFIX = "embedding_gate_calibration:"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _best_threshold(scored: list[tuple[float, bool]]) -> tuple[float, float]:
    """Sweep candidate cutoffs; return (threshold, youden_j)."""
    positives = sum(1 for _, relevant in scored if relevant)
    negatives = len(scored) - positives
    if not positives or not negatives:
        raise SystemExit("探针集必须同时包含 relevant=true 和 relevant=false 的样本")
    best = (0.0, -1.0)
    for cut, _ in scored:
        tpr = sum(1 for s, r in scored if r and s >= cut) / positives
        fpr = sum(1 for s, r in scored if not r and s >= cut) / negatives
        j = tpr - fpr
        if j > best[1]:
            best = (cut, j)
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description="embedding gate calibration probe")
    parser.add_argument("--probes", type=Path, default=DEFAULT_PROBES)
    parser.add_argument("--force", action="store_true", help="重算并覆盖已存的校准结果")
    args = parser.parse_args()

    config = load_config()
    db.init_db()

    from core import vectorstore

    vectorstore.init_vectorstore(
        api_key=config["api_key"],
        base_url=config.get("base_url", "https://api.openai.com/v1"),
        embedding_model=config.get("embedding_model", "text-embedding-3-small"),
        embedding_base_url=config.get("embedding_base_url"),
        embedding_api_key=config.get("embedding_api_key"),
    )
    config_hash = vectorstore.current_embedding_config_hash() or "unknown"
    meta_key = f"{META_KEY_PREFIX}{config_hash}"

    stored = db.query_one("SELECT value FROM meta WHERE key = ?", (meta_key,))
    if stored is not None and not args.force:
        print(f"该 embedding 配置已校准（--force 重算）：\n{stored['value']}")
        return

    payload = json.loads(args.probes.read_text(encoding="utf-8"))
    probes = payload.get("probes") or []
    if len(probes) < 8:
        raise SystemExit("探针集太小（至少 8 对），校准不可信")

    from chromadb.utils.embedding_functions.openai_embedding_function import (
        OpenAIEmbeddingFunction,
    )

    embed = OpenAIEmbeddingFunction(
        api_key=config.get("embedding_api_key") or config["api_key"],
        api_base=config.get("embedding_base_url") or config.get("base_url", "https://api.openai.com/v1"),
        model_name=config.get("embedding_model", "text-embedding-3-small"),
    )
    texts = [p["query"] for p in probes] + [p["content"] for p in probes]
    vectors = embed(texts)
    n = len(probes)
    scored = [
        (_cosine(list(vectors[i]), list(vectors[n + i])), bool(probes[i]["relevant"]))
        for i in range(n)
    ]

    threshold, youden = _best_threshold(scored)
    result = {
        "embedding_config_hash": config_hash,
        "embedding_model": config.get("embedding_model", "text-embedding-3-small"),
        "probe_count": n,
        "youden_threshold": round(threshold, 4),
        "youden_j": round(youden, 4),
        "suggested_fallback_floor": round(threshold, 2),
        "suggested_hard_floor": round(max(0.05, threshold - 0.10), 2),
    }
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (meta_key, json.dumps(result, ensure_ascii=False)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        "\n把 suggested_fallback_floor / suggested_hard_floor 更新到 "
        "core/memory_read.py 的 SEMANTIC_SIM_FALLBACK_FLOOR / SEMANTIC_SIM_HARD_FLOOR。"
    )


if __name__ == "__main__":
    main()
