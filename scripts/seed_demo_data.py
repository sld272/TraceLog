#!/usr/bin/env python3
"""Generate realistic demo data through the normal TraceLog pipeline.

演示数据的全部文字（帖子 / 追问 / 私聊）放在同目录的 TOML 内容文件里，本脚本只负责
按正常管线把它们灌进 workspace。内容文件查找顺序：

1. ``--content <path>`` 指定的文件；
2. ``scripts/demo_content.toml``（你的真实内容，已 gitignore，自由发挥）；
3. ``scripts/demo_content.example.toml``（仓库自带模板，开箱即用）。

内容按真实校历时间顺序逐条喂入：全局深反思每 3 帖触发一次、且只对账"上次反思之后"的帖子，
所以 user.md 会先被写入"自我怀疑"，再在后续成功证据累积时被 reconcile 成"确认方向"——成长画像
的 revise 是机制自然跑出来的。每个 SOUL 的深反思按"该人格每满 3 轮真实互动触发一次"的节奏排队。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.deps import get_runtime, init_runtime, shutdown_runtime
from core import chat_service, comment_service, db, record_service, tool_config_service
from core.app_services import event_service, job_service, public_post_pipeline

SCRIPT_DIR = Path(__file__).resolve().parent
CONTENT_FILENAME = "demo_content.toml"
EXAMPLE_FILENAME = "demo_content.example.toml"

DEFAULT_SEED = 20260612
DEFAULT_YEAR = 2026
DEMO_DEEP_REFLECTION_INTERVAL = 3
DEMO_SOUL_REFLECTION_ROUND_INTERVAL = 3


@dataclass(frozen=True)
class PostSpec:
    key: str
    month: int
    day: int
    hour: int
    minute: int
    act: str
    content: str


@dataclass(frozen=True)
class CommentThreadSpec:
    post_key: str
    soul_name: str
    followup: str


@dataclass(frozen=True)
class ChatSpec:
    soul_name: str
    message: str


@dataclass(frozen=True)
class DemoContent:
    posts: list[PostSpec]
    comment_threads: list[CommentThreadSpec]
    chats: list[ChatSpec]


@dataclass(frozen=True)
class DemoPostPlan:
    key: str
    created_at: datetime
    act: str
    content: str


@dataclass(frozen=True)
class SeedStats:
    posts_created: int
    post_timeouts: int
    comment_threads_created: int
    comment_threads_skipped: int
    chat_messages_created: int
    comments_backfilled: int


# ---------------------------------------------------------------------------
# 内容加载（TOML）
# ---------------------------------------------------------------------------


def resolve_content_path(explicit: str | None = None) -> Path:
    """按 --content → demo_content.toml → demo_content.example.toml 顺序定位内容文件。"""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"指定的内容文件不存在：{path}")
        return path
    real = SCRIPT_DIR / CONTENT_FILENAME
    if real.exists():
        return real
    example = SCRIPT_DIR / EXAMPLE_FILENAME
    if example.exists():
        return example
    raise FileNotFoundError(
        f"找不到内容文件：请把 {EXAMPLE_FILENAME} 复制为 {CONTENT_FILENAME} 后编辑（目录 {SCRIPT_DIR}）"
    )


def load_content(path: Path) -> DemoContent:
    """读取并校验内容文件；任何结构错误都抛出带定位的 ValueError。"""
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    posts = [_parse_post(item, index) for index, item in enumerate(data.get("posts", []))]
    threads = [_parse_thread(item, index) for index, item in enumerate(data.get("comment_threads", []))]
    chats = [_parse_chat(item, index) for index, item in enumerate(data.get("chats", []))]
    _validate(posts, threads, path)
    return DemoContent(posts=posts, comment_threads=threads, chats=chats)


def _parse_post(item: dict, index: int) -> PostSpec:
    try:
        return PostSpec(
            key=str(item["key"]),
            month=int(item["month"]),
            day=int(item["day"]),
            hour=int(item["hour"]),
            minute=int(item["minute"]),
            act=str(item.get("act", "")),
            content=str(item["content"]),
        )
    except KeyError as exc:
        raise ValueError(f"posts[{index}] 缺少必填字段 {exc}") from exc


def _parse_thread(item: dict, index: int) -> CommentThreadSpec:
    try:
        return CommentThreadSpec(
            post_key=str(item["post_key"]),
            soul_name=str(item["soul_name"]),
            followup=str(item["followup"]),
        )
    except KeyError as exc:
        raise ValueError(f"comment_threads[{index}] 缺少必填字段 {exc}") from exc


def _parse_chat(item: dict, index: int) -> ChatSpec:
    try:
        return ChatSpec(soul_name=str(item["soul_name"]), message=str(item["message"]))
    except KeyError as exc:
        raise ValueError(f"chats[{index}] 缺少必填字段 {exc}") from exc


def _validate(posts: list[PostSpec], threads: list[CommentThreadSpec], path: Path) -> None:
    if not posts:
        raise ValueError(f"{path} 里没有任何 [[posts]]")
    keys = [post.key for post in posts]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"posts 的 key 重复：{duplicates}")
    key_set = set(keys)
    orphans = sorted({thread.post_key for thread in threads if thread.post_key not in key_set})
    if orphans:
        raise ValueError(f"comment_threads 指向不存在的 post_key：{orphans}")


def build_demo_plan(posts: list[PostSpec], *, year: int = DEFAULT_YEAR, limit: int = 0) -> list[DemoPostPlan]:
    plans: list[DemoPostPlan] = []
    for post in posts:
        created_at = datetime(year, post.month, post.day, post.hour, post.minute, 0).astimezone()
        plans.append(DemoPostPlan(key=post.key, created_at=created_at, act=post.act, content=post.content))
    plans.sort(key=lambda item: item.created_at)
    if limit and limit > 0:
        plans = plans[:limit]
    return plans


# ---------------------------------------------------------------------------
# CLI / 主流程
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate TraceLog demo data through the full pipeline.")
    parser.add_argument("--content", default=None, help="内容 TOML 文件路径；默认 demo_content.toml，回退到示例")
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR, help="剧本所用年份（默认 2026）")
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 条（按时间序），0 表示全部，用于快速试跑")
    parser.add_argument("--comment-threads", type=int, default=-1, help="使用前 N 条追问，-1 表示全部")
    parser.add_argument("--chat-rounds", type=int, default=-1, help="使用前 N 条私聊，-1 表示全部")
    parser.add_argument("--batch-size", type=int, default=1, help="每批发多少条再等管线，默认 1（让深反思按节奏触发）")
    parser.add_argument("--timeout-per-post", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    try:
        content_path = resolve_content_path(args.content)
        content = load_content(content_path)
    except (FileNotFoundError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"内容文件加载失败：{exc}")
        return 2

    plan = build_demo_plan(content.posts, year=args.year, limit=args.limit)
    print(f"内容来源：{content_path}")
    if args.dry_run:
        print_plan(plan, content)
        return 0

    if not args.yes and not confirm_write(plan, content):
        print("已取消。")
        return 1

    runtime = await init_runtime()
    if not runtime.configured or runtime.client is None or runtime.model is None:
        await shutdown_runtime()
        print("模型配置缺失：请先在 Web 设置页或 config.json 中配置主模型和 embedding。")
        return 2

    try:
        stats = await run_seed(plan, content, args)
        print_stats(stats)
        return 0
    finally:
        await shutdown_runtime()


async def run_seed(plan: list[DemoPostPlan], content: DemoContent, args: argparse.Namespace) -> SeedStats:
    runtime = get_runtime()
    if runtime.client is None or runtime.model is None:
        raise RuntimeError("runtime 未配置模型，无法生成数据")
    client = runtime.client
    model = runtime.model
    rng = random.Random(args.seed)
    created_post_ids: list[str] = []
    post_id_by_key: dict[str, str] = {}
    post_timeouts = 0
    batch_size = max(1, min(int(args.batch_size), 10))

    # 1) 按时间序逐条喂入；每三条排一次全局深反思，让 reconcile 有节奏地发生。
    for index in range(0, len(plan), batch_size):
        batch = plan[index : index + batch_size]
        batch_ids = []
        for item in batch:
            post_number = len(created_post_ids) + 1
            created = create_demo_post(
                item.content,
                created_at=item.created_at,
                trigger_global_deep_reflection=(post_number % DEMO_DEEP_REFLECTION_INTERVAL == 0),
            )
            batch_ids.append(created.post_id)
            created_post_ids.append(created.post_id)
            post_id_by_key[item.key] = created.post_id
            print(f"[post] {created.post_id} {item.created_at.isoformat(timespec='minutes')} [{item.act}]")
        for post_id in batch_ids:
            ok = await wait_for_post_pipeline(post_id, timeout=float(args.timeout_per_post))
            if not ok:
                post_timeouts += 1
                print(f"[post] {post_id} 等待超时，继续后续数据生成。")

    # 2) 针对性追问：在 SOUL 首评下继续一轮真实对话（按稳定 key 绑定帖子）。
    rounds_by_soul: dict[str, int] = {}
    comment_created, comment_skipped = await create_comment_threads(
        post_id_by_key,
        _cap(content.comment_threads, args.comment_threads),
        client,
        model,
        rounds_by_soul,
    )

    # 3) 私聊：保持近期时间戳，展示"当前在用"。
    chat_created = await create_chat_rounds(_cap(content.chats, args.chat_rounds), client, model, rounds_by_soul)

    # 4) 回填评论时间，避免历史帖下面挂着"今天"的评论而穿帮（私聊不回填）。
    comments_backfilled = backfill_comment_times(rng)

    # 5) 收尾：补 soul 深反思 + 全局深反思，确保最近的尾巴也被对账进长期记忆。
    enqueue_final_reflections()
    await wait_until_no_pending_jobs(timeout=900.0)

    return SeedStats(
        posts_created=len(created_post_ids),
        post_timeouts=post_timeouts,
        comment_threads_created=comment_created,
        comment_threads_skipped=comment_skipped,
        chat_messages_created=chat_created,
        comments_backfilled=comments_backfilled,
    )


async def wait_for_post_pipeline(post_id: str, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max(1.0, timeout)
    while asyncio.get_running_loop().time() < deadline:
        status = public_post_pipeline.summarize_pipeline_status(post_id)
        if status["state"] in {"done", "failed"}:
            return True
        await asyncio.sleep(1.0)
    return False


async def wait_until_no_pending_jobs(*, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max(1.0, timeout)
    while asyncio.get_running_loop().time() < deadline:
        pending = job_service.list_jobs(status=job_service.STATUS_PENDING, limit=1)
        running = job_service.list_jobs(status=job_service.STATUS_RUNNING, limit=1)
        if not pending and not running:
            return True
        await asyncio.sleep(1.0)
    return False


async def create_comment_threads(
    post_id_by_key: dict[str, str],
    specs: list[CommentThreadSpec],
    client,
    model: str,
    rounds_by_soul: dict[str, int],
) -> tuple[int, int]:
    created = 0
    skipped = 0
    for spec in specs:
        post_id = post_id_by_key.get(spec.post_key)
        if post_id is None:
            skipped += 1
            print(f"[comment] 跳过：找不到 post_key={spec.post_key}（本次未创建或被 --limit 截断）")
            continue
        try:
            result = await asyncio.to_thread(
                comment_service.call_comment_reply,
                post_id,
                spec.soul_name,
                spec.followup,
                client,
                model,
            )
        except ValueError as exc:
            skipped += 1
            print(f"[comment] 跳过 {post_id}/{spec.soul_name}: {exc}")
            continue
        if result.ok:
            created += 1
            note_soul_round_and_maybe_reflect(spec.soul_name, rounds_by_soul)
            print(f"[comment] {post_id}/{spec.soul_name} 已生成追问回复")
        else:
            skipped += 1
            print(f"[comment] {post_id}/{spec.soul_name} 生成失败: {result.error}")
    return created, skipped


async def create_chat_rounds(specs: list[ChatSpec], client, model: str, rounds_by_soul: dict[str, int]) -> int:
    created = 0
    for spec in specs:
        try:
            thread = await asyncio.to_thread(chat_service.get_or_create_thread, spec.soul_name)
            result = await asyncio.to_thread(chat_service.call_chat_reply, thread.id, spec.message, client, model)
        except ValueError as exc:
            print(f"[chat] 跳过 {spec.soul_name}: {exc}")
            continue
        if result.ok:
            created += 1
            note_soul_round_and_maybe_reflect(spec.soul_name, rounds_by_soul)
            print(f"[chat] {spec.soul_name} 已生成一轮私聊")
        else:
            print(f"[chat] {spec.soul_name} 生成失败: {result.error}")
    return created


def backfill_comment_times(rng: random.Random) -> int:
    """把每条评论的 created_at 回填到对应 post 时间附近，按 (post, soul, seq) 递增加 jitter。"""
    post_rows = db.query_all("SELECT id, created_at FROM posts")
    post_ts = {row["id"]: float(row["created_at"]) for row in post_rows}
    comment_rows = db.query_all(
        "SELECT id, post_id, soul_name, seq FROM comments ORDER BY post_id, soul_name, seq"
    )
    updates: list[tuple[float, int]] = []
    cursor_ts: dict[tuple[str, str], float] = {}
    for row in comment_rows:
        base = post_ts.get(row["post_id"])
        if base is None:
            continue
        key = (row["post_id"], row["soul_name"])
        if int(row["seq"]) == 0:
            # 首评：发帖后 2–30 分钟。
            ts = base + rng.randint(2, 30) * 60
        else:
            # 追问/回复：在该线程上一条之后 15–150 分钟。
            prev = cursor_ts.get(key, base + 20 * 60)
            ts = prev + rng.randint(15, 150) * 60
        cursor_ts[key] = ts
        updates.append((ts, int(row["id"])))

    if not updates:
        return 0
    with db.transaction() as conn:
        conn.executemany("UPDATE comments SET created_at = ? WHERE id = ?", updates)
    return len(updates)


def create_demo_post(
    content: str,
    *,
    created_at: datetime,
    trigger_global_deep_reflection: bool,
) -> public_post_pipeline.CreatedPost:
    body = content.strip()
    if not body:
        raise ValueError("demo post content 不能为空")

    post_id = record_service.save_post(
        body,
        index_immediately=False,
        track_embedding=True,
        created_at=created_at,
    )
    event_service.append_post_event(post_id, "post_created", {"post_id": post_id})

    job_ids = [
        job_service.enqueue(job_service.TYPE_INDEX_POST_EMBEDDING, {"post_id": post_id}),
        job_service.enqueue(job_service.TYPE_GENERATE_POST_REPLIES, {"post_id": post_id, "content": body}),
    ]
    if tool_config_service.is_tool_enabled("todo"):
        job_ids.append(job_service.enqueue(job_service.TYPE_RUN_TODO_TOOL, {"post_id": post_id}))
    job_ids.append(job_service.enqueue(job_service.TYPE_RUN_LIGHT_REFLECTION, {"post_id": post_id}))
    if trigger_global_deep_reflection:
        job_ids.append(
            job_service.enqueue(
                job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION,
                {"trigger": "demo_seed_interval", "limit": 100},
            )
        )
    return public_post_pipeline.CreatedPost(post_id=post_id, job_ids=job_ids)


def note_soul_round_and_maybe_reflect(soul_name: str, rounds_by_soul: dict[str, int]) -> int | None:
    """记一轮该人格的真实互动（一轮 = 一次追问/私聊来回）；每满 N 轮，只为该人格排一次深反思。"""
    rounds_by_soul[soul_name] = rounds_by_soul.get(soul_name, 0) + 1
    if rounds_by_soul[soul_name] % DEMO_SOUL_REFLECTION_ROUND_INTERVAL != 0:
        return None
    return job_service.enqueue(
        job_service.TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS,
        {
            "trigger": "demo_seed_interval",
            "limit_per_soul": 100,
            "soul_names": [soul_name],
        },
    )


def enqueue_final_reflections() -> None:
    if not _has_unfinished_job(job_service.TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS):
        job_service.enqueue(
            job_service.TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS,
            {"trigger": "demo_seed", "limit_per_soul": 100},
        )
    if not _has_unfinished_job(job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION):
        job_service.enqueue(
            job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION,
            {"trigger": "demo_seed"},
        )


def _has_unfinished_job(job_type: str) -> bool:
    for status in (job_service.STATUS_PENDING, job_service.STATUS_RUNNING):
        if job_service.list_jobs(status=status, job_type=job_type, limit=1):
            return True
    return False


def _cap(items: list, count: int) -> list:
    if count is None or count < 0:
        return list(items)
    return list(items[:count])


def print_plan(plan: list[DemoPostPlan], content: DemoContent) -> None:
    print(f"将生成 {len(plan)} 条公开记录。")
    if plan:
        print(
            f"时间范围：{plan[0].created_at.isoformat(timespec='minutes')}"
            f" -> {plan[-1].created_at.isoformat(timespec='minutes')}"
        )
    print("分幕分布：")
    for act, count in Counter(item.act for item in plan).most_common():
        print(f"  {act or '(未标注)'}: {count}")
    print(f"追问线程：{len(content.comment_threads)}    私聊：{len(content.chats)}")
    print("\n全部帖子预览：")
    for index, item in enumerate(plan):
        one_line = item.content.replace("\n", " ")[:48]
        print(f"  [{index:>2}] {item.created_at.isoformat(timespec='minutes')} ({item.key}) {one_line}…")


def confirm_write(plan: list[DemoPostPlan], content: DemoContent) -> bool:
    print_plan(plan, content)
    print("\n将追加写入当前 workspace，并真实调用 LLM/Embedding，可能产生费用且耗时较久。")
    print(f"workspace: {db.WORKSPACE_DIR}")
    answer = input("输入 YES 继续：").strip()
    return answer == "YES"


def print_stats(stats: SeedStats) -> None:
    counts = {
        "posts": _count("posts"),
        "comments": _count("comments"),
        "chat_messages": _count("chat_messages"),
        "todos": _count("todos"),
        "reflections": _count("reflections"),
    }
    failed_jobs = job_service.list_jobs(status=job_service.STATUS_FAILED, limit=20)
    print("\n完成。")
    print(f"本次创建 posts: {stats.posts_created}")
    print(f"post pipeline 等待超时: {stats.post_timeouts}")
    print(f"评论追问成功/跳过: {stats.comment_threads_created}/{stats.comment_threads_skipped}")
    print(f"私聊轮次成功: {stats.chat_messages_created}")
    print(f"回填评论时间: {stats.comments_backfilled} 条")
    print("当前数据库统计：")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    print(f"失败 jobs: {len(failed_jobs)}")
    for job in failed_jobs[:10]:
        print(f"  #{job['id']} {job['type']}: {job.get('error')}")


def _count(table: str) -> int:
    row = db.query_one(f"SELECT COUNT(*) AS count FROM {table}")
    return int(row["count"]) if row is not None else 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
