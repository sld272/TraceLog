"""Interactive CLI sessions and memory-v2 post-processing helpers."""

from __future__ import annotations

from core import (
    chat_service,
    comment_service,
    logging_service,
    memory_reconcile_runner,
    memory_view_producer,
    todo_service,
    tool_config_service,
    vector_index_service,
)
from core.cli_input import read_cli_input
from core.llm.types import LLMClient


def run_memory_reconcile(client: LLMClient, model: str, *, trigger: str) -> None:
    print("\n[记忆] 正在对账新增证据...")
    try:
        result = memory_reconcile_runner.run_pending_reconcile(
            client,
            model,
            trigger=trigger,
        )
        views = memory_view_producer.refresh_views_after_reconcile(client, model)
        vector_index_service.rebuild_expected_docs()
        vector_index_service.process_outbox()
        if result.failures or result.relink_failures:
            errors = [failure.error for failure in result.failures]
            errors.extend(failure.error for failure in result.relink_failures)
            raise RuntimeError("; ".join(errors))
        applied = sum(summary.applied for summary in result.summaries)
        logging_service.log_event(
            "memory_reconcile_completed",
            trigger=trigger,
            bucket_count=len(result.summaries),
            applied=applied,
            refreshed_view_count=len(views),
        )
        if result.summaries or views:
            print(
                f"[记忆] 已完成 {len(result.summaries)} 个 bucket 的对账，"
                f"应用 {applied} 个操作，刷新 {len(views)} 个视图。"
            )
        else:
            print("[记忆] 没有待处理证据。")
    except KeyboardInterrupt:
        logging_service.log_event("memory_reconcile_interrupted", level="WARNING", trigger=trigger)
        print("\n[警告] 记忆对账被中断，未消费的证据会保留。")
    except Exception as e:
        logging_service.log_event(
            "memory_reconcile_failed", level="ERROR", trigger=trigger, error=str(e)
        )
        print(f"[记忆] 对账失败：{e}；未消费的证据会保留。")


def run_todo_tool_for_post(post_id: str, client: LLMClient, model: str) -> None:
    if not tool_config_service.is_tool_enabled("todo"):
        return
    result = todo_service.run_for_post_safely(post_id, client, model)
    if result.error:
        print(f"[TodoTool] 待办抽取暂时失败：{result.error}\n")
    elif result.applied:
        print(f"[TodoTool] 待办已更新：新增/更新 {result.upserted} 条，删除 {result.deleted} 条。\n")


def run_comment_session(
    conversation: comment_service.CommentConversation,
    client: LLMClient,
    model: str,
    todos: list,
) -> tuple[list, bool]:
    print(
        f"\n[评论] 已进入 post {conversation.post_id} 下与 {conversation.soul_name} 的评论对话。"
        "输入 /back 返回发帖模式，/quit 退出。\n"
    )
    current_todos = todos
    while True:
        try:
            raw_input = read_cli_input(f"[{conversation.soul_name} 评论] 你: ")
        except (KeyboardInterrupt, EOFError):
            return current_todos, True
        user_message = raw_input.strip()
        if not user_message:
            continue
        if user_message == "/back":
            print("[评论] 已返回发帖模式。\n")
            return current_todos, False
        if user_message == "/quit":
            return current_todos, True

        print(f"\n[{thread.soul_name} 正在回复评论...]\n")
        try:
            result = comment_service.call_comment_reply(conversation.post_id, conversation.soul_name, user_message, client, model)
        except ValueError as exc:
            print(f"[评论] {exc}\n")
            return current_todos, False

        if result.ok:
            print(f"[{result.soul_name}] {result.reply}\n")
        else:
            print(f"[{result.soul_name}] {result.reply}（{result.error}）\n")


def run_chat_session(
    thread: chat_service.ChatThread,
    client: LLMClient,
    model: str,
    todos: list,
) -> tuple[list, bool]:
    print(f"\n[私聊] 已进入与 {thread.soul_name} 的私聊。输入 /back 返回发帖模式，/quit 退出。\n")
    current_todos = todos
    while True:
        try:
            raw_input = read_cli_input(f"[{thread.soul_name}] 你: ")
        except (KeyboardInterrupt, EOFError):
            return current_todos, True
        user_message = raw_input.strip()
        if not user_message:
            continue
        if user_message == "/back":
            print("[私聊] 已返回发帖模式。\n")
            return current_todos, False
        if user_message == "/quit":
            return current_todos, True

        print(f"\n[{thread.soul_name} 正在回复...]\n")
        try:
            result = chat_service.call_chat_reply(thread.id, user_message, client, model)
        except ValueError as exc:
            print(f"[私聊] {exc}\n")
            return current_todos, False

        if result.ok:
            print(f"[{result.soul_name}] {result.reply}\n")
        else:
            print(f"[{result.soul_name}] {result.reply}（{result.error}）\n")
