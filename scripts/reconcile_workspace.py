"""Run (or preview) memory-v2 reconcile over your real workspace.

This exercises the new write pipeline end-to-end against your configured LLM:
posts/comments/chat -> evidence events -> per-bucket reconcile -> memory units.

DEFAULT IS DRY-RUN: it shows the units it WOULD extract and persists nothing.
Pass --commit to actually write units. Pass --views to also (re)synthesize the
user.md / soul-private identity views from the resulting core units (implies a
commit having happened).

Usage (from repo root):
    conda run -n tracelog python -m scripts.reconcile_workspace            # dry-run preview
    conda run -n tracelog python -m scripts.reconcile_workspace --commit   # actually write units
    conda run -n tracelog python -m scripts.reconcile_workspace --commit --views

Nothing here touches the live reply path or the legacy reflection pipeline.
"""

from __future__ import annotations

import argparse

from openai import OpenAI

from core import db, logging_service, memory_reconcile_runner as runner
from core.cli.config import load_config


def _print_summary(summary, *, dry_run: bool) -> None:
    bucket = f"{summary.owner_scope} | {summary.visibility_scope}"
    print("\n" + "=" * 72)
    print(f"bucket: {bucket}")
    print(
        f"  events consumed: {summary.event_count}"
        f"   applied: {summary.applied}   skipped: {summary.skipped}"
        f"   by_op: {summary.by_op or '{}'}"
    )
    if summary.summary_text:
        print(f"  模型摘要: {summary.summary_text}")
    if summary.skipped_details:
        print(f"  跳过明细: {summary.skipped_details}")

    if dry_run:
        units = summary.preview_units
        if not units:
            print("  (本批不会产生/改变任何 active unit)")
        else:
            print(f"  —— 拟得到的 active units（共 {len(units)}，预览不落库）——")
            for unit in units:
                print(
                    f"   · [{unit['type']}|{unit['tier']}] "
                    f"conf={unit['confidence']:.2f} imp={unit['importance']:.2f}"
                )
                print(f"     {unit['content']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="memory-v2 workspace reconcile")
    parser.add_argument("--commit", action="store_true", help="实际写入 units（默认只预览）")
    parser.add_argument("--views", action="store_true", help="提交后用 core units 重综合 user.md/soul 画像")
    parser.add_argument("--limit", type=int, default=200, help="每个 bucket 单次消费的最大事件数")
    args = parser.parse_args()

    config = load_config()
    logging_service.init_logging(config.get("logging"))

    # init_db creates the memory-v2 tables and backfills evidence events for any
    # existing posts/comments/chat (idempotent).
    db.init_db()

    client = OpenAI(
        api_key=config["api_key"],
        base_url=config.get("base_url", "https://api.openai.com/v1"),
    )
    model = config["model"]
    dry_run = not args.commit

    mode = "预览（dry-run，不落库）" if dry_run else "正式提交（写入 units）"
    print(f"模型: {model}  |  模式: {mode}")

    result = runner.run_pending_reconcile(
        client, model, dry_run=dry_run, trigger="manual_script", limit_per_bucket=args.limit
    )
    summaries = result.summaries

    if not summaries and not result.failures:
        print("\n没有待对账的 bucket（所有事件都已消费，或 workspace 为空）。")
        return

    total_applied = 0
    for summary in summaries:
        _print_summary(summary, dry_run=dry_run)
        total_applied += summary.applied

    print("\n" + "=" * 72)
    print(f"共处理 {len(summaries)} 个 bucket，{'拟' if dry_run else '已'}应用 {total_applied} 个 op。")
    if result.failures:
        print(f"失败 bucket（共 {len(result.failures)} 个）：")
        for failure in result.failures:
            print(f"  - {failure.owner_scope} | {failure.visibility_scope}: {failure.error}")
    if dry_run:
        print("这是预览。确认质量后，加 --commit 实际写入。")
    elif result.failures:
        raise SystemExit(1)

    if args.views and not dry_run:
        _synthesize_views(client, model)


def _print_view_body(content_md: str) -> None:
    import re

    body = re.sub(r"<!--.*?-->", "", content_md, flags=re.DOTALL).strip()
    for line in body.splitlines():
        print(f"    │ {line}")


def _synthesize_views(client, model) -> None:
    """Re-synthesize identity views from the freshly written core units.

    The v2 portrait lives in the memory_views table (not a workspace file —
    user.md is non-editable in v2, so a file would only be a stale duplicate).
    We print it here so it's inspectable."""
    from core import db, memory_view_service as mvs
    from core.memory_view_producer import make_llm_synthesizer

    print("\n[views] 重综合身份画像（存入 memory_views 表，不写 workspace 文件）...")
    # global+public -> user portrait view
    synth = make_llm_synthesizer(client, model, mvs.VIEW_USER_MD)
    view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD, synthesizer=synth)
    label = "模板兜底" if view.used_fallback else "LLM 综合"
    print(f"  用户画像（{label}，{len(view.unit_ids)} 个 core units）：")
    _print_view_body(view.content_md)

    # each soul with private-chat units -> soul private memory view (soul's view of the user)
    soul_owners = db.query_all(
        """
        SELECT DISTINCT owner_scope, visibility_scope
        FROM memory_units
        WHERE owner_scope LIKE 'soul:%' AND visibility_scope LIKE 'private:soul:%'
              AND status = 'active'
        """
    )
    for row in soul_owners:
        owner, vis = row["owner_scope"], row["visibility_scope"]
        synth = make_llm_synthesizer(client, model, mvs.VIEW_SOUL_PRIVATE)
        v = mvs.synthesize_view(owner, vis, mvs.VIEW_SOUL_PRIVATE, synthesizer=synth)
        label = "模板兜底" if v.used_fallback else "LLM 综合"
        print(f"\n  {owner} 私聊画像（{label}，{len(v.unit_ids)} 个 core units）：")
        _print_view_body(v.content_md)


if __name__ == "__main__":
    main()
