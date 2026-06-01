"""Interactive CLI sessions and post-processing display helpers."""

from __future__ import annotations

from core import chat_service, comment_service, logging_service, reflector, todo_service, tool_config_service
from core.cli_input import read_cli_input
from core.llm.types import LLMClient


def run_deep_reflection_on_exit(client: LLMClient, model: str) -> None:
    print("\n\n[反思] 正在整理本次记录与 SOUL 互动，请稍候（请勿再次终止）...")
    try:
        try:
            scope = reflector.preview_global_deep_reflection_scope()
        except Exception:
            scope = None
        if scope is not None and scope.post_ids:
            print(f"[反思] 检测到 {len(scope.post_ids)} 条尚未深反思的公开记录，正在反思。")
        result = reflector.trigger_global_deep_reflection(client, model, trigger="cli_exit")
        if result is None:
            print("[反思] 没有新的公开记录，已跳过本次深反思。")
        else:
            logging_service.log_event(
                "deep_reflection_saved",
                reflection_id=result.id,
                related_post_ids=result.related_post_ids,
                patch_summary=result.patch_summary,
            )
            print(
                f"[反思] 深反思已保存（id={result.id}，覆盖 {len(result.related_post_ids)} 条记录，"
                f"画像更新 applied={result.patch_summary.get('applied', 0)} "
                f"skipped={result.patch_summary.get('skipped', 0)}）。"
            )
    except KeyboardInterrupt:
        logging_service.log_event("deep_reflection_interrupted", level="WARNING", type="global")
        print("\n[警告] 深反思被强制中断，已有数据保持不变。")
    except Exception as e:
        logging_service.log_event("deep_reflection_failed", level="ERROR", type="global", error=str(e))
        print(f"[反思] 深反思失败：{e}，已有数据保持不变。")
    try:
        try:
            soul_scopes = reflector.preview_soul_deep_reflection_scopes()
        except Exception:
            soul_scopes = []
        soul_interaction_count = sum(scope.interaction_count for scope in soul_scopes)
        if soul_interaction_count:
            print(f"[反思] 检测到 {soul_interaction_count} 条尚未沉淀的 SOUL 互动，正在反思。")
        soul_results = reflector.trigger_soul_deep_reflections(client, model, trigger="cli_exit")
        if soul_results:
            applied = sum(item.patch_summary.get("applied", 0) for item in soul_results)
            skipped = sum(item.patch_summary.get("skipped", 0) for item in soul_results)
            logging_service.log_event(
                "deep_reflection_saved",
                type="soul",
                count=len(soul_results),
                applied=applied,
                skipped=skipped,
            )
            print(
                f"[反思] SOUL 深反思已保存 {len(soul_results)} 份，"
                f"独立画像更新 applied={applied} skipped={skipped}。"
            )
        elif soul_interaction_count:
            logging_service.log_event(
                "deep_reflection_failed",
                level="WARNING",
                type="soul",
                reason="no_saved_results",
                pending_interaction_count=soul_interaction_count,
            )
            print("[反思] SOUL 深反思未保存：检测到互动，但本次没有生成有效结果，已保留待下次重试。")
        else:
            print("[反思] 没有新的 SOUL 互动，已跳过 SOUL 深反思。")
    except KeyboardInterrupt:
        logging_service.log_event("deep_reflection_interrupted", level="WARNING", type="soul")
        print("\n[警告] SOUL 深反思被强制中断，已有数据保持不变。")
    except Exception as e:
        logging_service.log_event("deep_reflection_failed", level="ERROR", type="soul", error=str(e))
        print(f"[反思] SOUL 深反思失败：{e}，已有数据保持不变。")
    print("再见！\n")


def run_light_reflection_for_post(post_id: str, client: LLMClient, model: str) -> None:
    light_result = reflector.run_light_reflection_safely(post_id, client, model)
    if light_result is None:
        print("[反思] 轻反思暂时失败，已加入待重试队列。\n")
    else:
        print(
            "[反思] 轻反思已完成："
            f"{len(light_result.entities)} 个实体，"
            f"{len(light_result.emotions)} 个情绪，"
            f"{len(light_result.events)} 个事件。\n"
        )


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
