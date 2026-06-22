"""Preview or run memory-v2 reconciliation for the configured workspace."""

from __future__ import annotations

import argparse

from openai import OpenAI

from core import (
    db,
    logging_service,
    memory_reconcile_runner,
    memory_view_producer,
    vector_index_service,
)
from core.cli.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="memory-v2 workspace reconcile")
    parser.add_argument("--commit", action="store_true", help="写入 unit；默认仅预览")
    parser.add_argument("--limit", type=int, default=200, help="每个 bucket 的事件上限")
    args = parser.parse_args()

    config = load_config()
    logging_service.init_logging(config.get("logging"))
    db.init_db()
    client = OpenAI(
        api_key=config["api_key"],
        base_url=config.get("base_url", "https://api.openai.com/v1"),
    )
    dry_run = not args.commit
    result = memory_reconcile_runner.run_pending_reconcile(
        client,
        config["model"],
        dry_run=dry_run,
        trigger="workspace_script",
        limit_per_bucket=args.limit,
    )

    for summary in result.summaries:
        print(
            f"{summary.owner_scope}/{summary.visibility_scope}: "
            f"events={summary.event_count} applied={summary.applied} "
            f"skipped={summary.skipped} ops={summary.by_op}"
        )
        if dry_run:
            for unit in summary.preview_units:
                print(f"  - [{unit['type']}] {unit['content']}")

    if result.failures or result.relink_failures:
        for failure in result.failures:
            print(
                f"FAILED {failure.owner_scope}/{failure.visibility_scope}: "
                f"{failure.error}"
            )
        for failure in result.relink_failures:
            print(f"FAILED relink {failure.unit_id}: {failure.error}")
        raise SystemExit(1)

    if not dry_run:
        views = memory_view_producer.refresh_views_after_reconcile(
            client, config["model"]
        )
        vector_index_service.rebuild_expected_docs()
        indexed = vector_index_service.process_outbox()
        print(f"refreshed_views={len(views)} indexed_docs={indexed}")


if __name__ == "__main__":
    main()
