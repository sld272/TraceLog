"""TraceLog CLI application loop."""

from __future__ import annotations

from openai import OpenAI

from core import context_builder, record_service, reply_service, retrieval, todo_service, tool_config_service
from core import vectorstore, workspace_service
from core.cli import commands, sessions
from core.cli.config import load_config
from core.cli_input import read_cli_input
from core import reflector


def main() -> None:
    print("\n" + "=" * 50)
    print("TraceLog 拾迹 ✦ 个人成长 AI 伴侣")
    print("=" * 50)

    try:
        config = load_config()
    except (ValueError, KeyboardInterrupt) as e:
        print(f"\n[错误] {e}")
        return

    client = OpenAI(
        api_key=config["api_key"],
        base_url=config.get("base_url", "https://api.openai.com/v1"),
    )
    model = config["model"]
    print(f"模型: {model}  |  Base URL: {config.get('base_url')}\n")

    try:
        workspace_service.init_workspace()
        vector_result = vectorstore.init_vectorstore(
            config["api_key"],
            config["base_url"],
            config["embedding_model"],
            config.get("embedding_base_url"),
            config.get("embedding_api_key"),
        )
        print(f"[向量存储] 初始化成功，已索引 {vector_result.indexed_count} 篇帖子。")
        fixed_embeddings = record_service.retry_pending_embeddings()
        if fixed_embeddings:
            print(f"[向量存储] 已补齐 {fixed_embeddings} 条待索引帖子。")
        fixed_reflections = reflector.retry_pending_light_reflections(client, model)
        if fixed_reflections:
            print(f"[反思] 已补跑 {fixed_reflections} 条轻反思。")
        todos = todo_service.load_todos() if tool_config_service.is_tool_enabled("todo") else []
    except vectorstore.VectorStoreInitError as e:
        print(f"[向量存储] 初始化失败：{e}")
        return
    except KeyboardInterrupt:
        print("\n[启动] 初始化被中断，已尽量回滚数据库事务。请重新运行。")
        return

    while True:
        try:
            raw_input = read_cli_input("你: ")
            user_input = raw_input.strip()
            if user_input.lower() == "/quit":
                raise KeyboardInterrupt
        except (KeyboardInterrupt, EOFError):
            sessions.run_deep_reflection_on_exit(client, model)
            break

        if not user_input:
            continue
        chat_handled, todos, quit_requested = commands.handle_chat_command(user_input, client, model, todos)
        if quit_requested:
            sessions.run_deep_reflection_on_exit(client, model)
            break
        if chat_handled:
            continue
        comment_handled, todos, quit_requested = commands.handle_comment_command(user_input, client, model, todos)
        if quit_requested:
            sessions.run_deep_reflection_on_exit(client, model)
            break
        if comment_handled:
            continue
        if commands.handle_tool_command(user_input):
            todos = todo_service.load_todos() if tool_config_service.is_tool_enabled("todo") else []
            continue
        if commands.handle_soul_command(user_input):
            continue

        relevant_ids = retrieval.hybrid_search(user_input, k=3)

        print("\n[TraceLog 正在思考...]\n")
        built_context = context_builder.build_context(relevant_post_ids=relevant_ids)

        post_id = record_service.save_post(user_input)
        sessions.run_todo_tool_for_post(post_id, client, model)

        results = reply_service.fanout(post_id, user_input, client, model, built_context)
        if not results:
            print("[TraceLog] 当前没有启用 SOUL，未生成评论。\n")
            sessions.run_light_reflection_for_post(post_id, client, model)
            continue

        for result in results:
            if result.ok:
                print(f"[{result.soul_name}] {result.reply}\n")
            else:
                print(f"[{result.soul_name}] {result.reply}（{result.error}）\n")

        sessions.run_light_reflection_for_post(post_id, client, model)

        if not any(result.ok for result in results):
            print("[TraceLog] 所有 SOUL 本次都回复失败，post 已保存，可稍后重试。\n")
            continue

        if tool_config_service.is_tool_enabled("todo"):
            todos = todo_service.load_todos()
