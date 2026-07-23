"""Capture and diff vector retrieval results across storage-engine revisions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
# 直接运行 `python scripts/vector_ab_compare.py ...` 时项目根目录不在 sys.path。
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core import db, vectorstore
from core.cli.config import load_config

FORMAT_VERSION = 1
SCORE_DRIFT_THRESHOLD = 1e-4
DOCUMENT_SURFACES: tuple[tuple[str, dict | None], ...] = (
    ("documents_all", None),
    ("documents_units", {"type": "unit"}),
    (
        "documents_evidence",
        {"type": {"$in": ["post", "post_vision", "comment", "chat"]}},
    ),
    ("documents_tombstones", {"type": "tombstone"}),
)


def capture(
    *,
    workspace: Path,
    queries_path: Path,
    out_path: Path,
    n_results: int = 20,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    state_db = workspace / "state.db"
    if not state_db.is_file():
        raise ValueError(f"找不到 SQLite 数据库：{state_db}")
    queries = _load_queries(queries_path)
    config = load_config()

    old_workspace, old_db_path = db.WORKSPACE_DIR, db.DB_PATH
    db.WORKSPACE_DIR = workspace
    db.DB_PATH = state_db
    try:
        initialized = vectorstore.init_vectorstore(
            api_key=config["api_key"],
            base_url=config["base_url"],
            embedding_model=config["embedding_model"],
            embedding_base_url=config.get("embedding_base_url"),
            embedding_api_key=config.get("embedding_api_key"),
        )
        captured_queries = [
            _capture_query(query_id, text, n_results=n_results)
            for query_id, text in queries
        ]
        payload = {
            "format_version": FORMAT_VERSION,
            "collection_name": initialized.collection_name,
            "queries": captured_queries,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload
    finally:
        db.WORKSPACE_DIR = old_workspace
        db.DB_PATH = old_db_path


def diff_captures(old_payload: dict[str, Any], new_payload: dict[str, Any]) -> str:
    if int(old_payload.get("format_version", 0)) != FORMAT_VERSION:
        raise ValueError("旧 capture 的格式版本不受支持")
    if int(new_payload.get("format_version", 0)) != FORMAT_VERSION:
        raise ValueError("新 capture 的格式版本不受支持")
    old_queries = {
        str(item["id"]): item
        for item in old_payload.get("queries", [])
    }
    new_queries = {
        str(item["id"]): item
        for item in new_payload.get("queries", [])
    }
    if set(old_queries) != set(new_queries):
        raise ValueError("两份 capture 的查询集合不一致")

    lines = [
        "# Vector A/B 对比",
        "",
        f"- 旧集合：`{old_payload.get('collection_name')}`",
        f"- 新集合：`{new_payload.get('collection_name')}`",
        f"- 查询数：{len(old_queries)}",
        "",
    ]
    total_added = 0
    total_lost = 0
    total_drift = 0
    changed_sections: list[str] = []
    for query_id in sorted(old_queries):
        old_query = old_queries[query_id]
        new_query = new_queries[query_id]
        if str(old_query.get("query")) != str(new_query.get("query")):
            raise ValueError(f"查询 {query_id} 的文本不一致")
        surface_changes: list[str] = []
        old_results = old_query.get("results") or {}
        new_results = new_query.get("results") or {}
        surfaces = sorted(set(old_results) | set(new_results))
        for surface in surfaces:
            old_hits = _hits_by_doc_id(old_results.get(surface) or [])
            new_hits = _hits_by_doc_id(new_results.get(surface) or [])
            added = sorted(set(new_hits) - set(old_hits))
            lost = sorted(set(old_hits) - set(new_hits))
            drifted = []
            for doc_id in sorted(set(old_hits) & set(new_hits)):
                old_distance = old_hits[doc_id].get("distance")
                new_distance = new_hits[doc_id].get("distance")
                if _distance_drifted(old_distance, new_distance):
                    drifted.append(
                        (
                            doc_id,
                            old_distance,
                            new_distance,
                            old_hits[doc_id].get("rank"),
                            new_hits[doc_id].get("rank"),
                        )
                    )
            total_added += len(added)
            total_lost += len(lost)
            total_drift += len(drifted)
            if not added and not lost and not drifted:
                continue
            surface_changes.append(f"#### `{surface}`")
            surface_changes.append("")
            if added:
                surface_changes.append("- 新增命中：" + ", ".join(f"`{doc_id}`" for doc_id in added))
            if lost:
                surface_changes.append("- 丢失命中：" + ", ".join(f"`{doc_id}`" for doc_id in lost))
            for doc_id, old_distance, new_distance, old_rank, new_rank in drifted:
                surface_changes.append(
                    f"- 分数漂移 `{doc_id}`：{_format_distance(old_distance)} → "
                    f"{_format_distance(new_distance)}（rank {old_rank} → {new_rank}）"
                )
            surface_changes.append("")
        if surface_changes:
            changed_sections.extend(
                [
                    f"### {query_id} · {old_query.get('query')}",
                    "",
                    *surface_changes,
                ]
            )

    lines.extend(
        [
            "## 汇总",
            "",
            f"- 新增命中：{total_added}",
            f"- 丢失命中：{total_lost}",
            f"- 分数漂移（>{SCORE_DRIFT_THRESHOLD:g}）：{total_drift}",
            "",
        ]
    )
    if changed_sections:
        lines.extend(["## 明细", "", *changed_sections])
    else:
        lines.extend(["两份结果无可报告差异。", ""])
    return "\n".join(lines)


def _capture_query(query_id: str, text: str, *, n_results: int) -> dict[str, Any]:
    results: dict[str, list[dict[str, Any]]] = {
        "post_hits": [
            {
                "doc_id": hit.post_id,
                "rank": hit.rank,
                "distance": hit.distance,
            }
            for hit in vectorstore.query_post_hits(text, n_results=n_results)
        ]
    }
    for surface, where in DOCUMENT_SURFACES:
        results[surface] = [
            {
                "doc_id": hit.doc_id,
                "rank": hit.rank,
                "distance": hit.distance,
            }
            for hit in vectorstore.query_documents(
                text,
                n_results=n_results,
                where=where,
            )
        ]
    return {"id": query_id, "query": text, "results": results}


def _load_queries(path: Path) -> list[tuple[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_queries = payload.get("queries") if isinstance(payload, dict) else payload
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("查询文件必须包含非空 queries 数组")
    queries: list[tuple[str, str]] = []
    for index, item in enumerate(raw_queries, start=1):
        if isinstance(item, str):
            query_id = f"q{index:02d}"
            text = item.strip()
        else:
            query_id = str(item["id"]).strip()
            text = str(item["query"]).strip()
        if not query_id or not text:
            raise ValueError(f"第 {index} 条查询缺少 id 或 query")
        queries.append((query_id, text))
    if len({query_id for query_id, _ in queries}) != len(queries):
        raise ValueError("查询 id 不得重复")
    return queries


def _hits_by_doc_id(hits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(hit["doc_id"]): hit for hit in hits}


def _distance_drifted(old_distance: Any, new_distance: Any) -> bool:
    if old_distance is None or new_distance is None:
        return old_distance != new_distance
    return abs(float(old_distance) - float(new_distance)) > SCORE_DRIFT_THRESHOLD


def _format_distance(value: Any) -> str:
    return "None" if value is None else f"{float(value):.8f}"


def _load_capture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"capture 不是 JSON 对象：{path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="capture or diff vector retrieval results")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--workspace", type=Path, required=True)
    capture_parser.add_argument("--queries", type=Path, required=True)
    capture_parser.add_argument("--out", type=Path, required=True)
    capture_parser.add_argument("--n-results", type=int, default=20)

    diff_parser = subparsers.add_parser("diff")
    diff_parser.add_argument("old", type=Path)
    diff_parser.add_argument("new", type=Path)

    args = parser.parse_args()
    if args.command == "capture":
        payload = capture(
            workspace=args.workspace,
            queries_path=args.queries,
            out_path=args.out,
            n_results=args.n_results,
        )
        print(f"已写入 {args.out}（{len(payload['queries'])} 条查询）")
        return
    print(diff_captures(_load_capture(args.old), _load_capture(args.new)))


if __name__ == "__main__":
    main()
