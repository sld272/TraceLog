"""TraceLog CLI application loop."""

from __future__ import annotations

from openai import OpenAI

from core import context_builder, logging_service, query_rewriter, record_service, reply_service, retrieval, todo_service, tool_config_service, vector_index_service
from core import vectorstore, workspace_service
from core.cli import commands, sessions
from core.cli.config import load_config
from core.cli_input import read_cli_input


def main() -> None:
    print("\n" + "=" * 50)
    print("TraceLog 拾迹 ✦ 个人成长 AI 伴侣")
    print("=" * 50)

    try:
        config = load_config()
    except (ValueError, KeyboardInterrupt) as e:
        print(f"\n[错误] {e}")
        return

    logging_service.init_logging(config.get("logging"))
    logging_service.log_event(
        "cli_start",
        model=config.get("model"),
        base_url=config.get("base_url"),
    )

    client = OpenAI(
        api_key=config["api_key"],
        base_url=config.get("base_url", "https://api.openai.com/v1"),
    )
    model = config["model"]
    print(f"模型: {model}  |  Base URL: {config.get('base_url')}\n")

    try:
        workspace_service.init_workspace()
        logging_service.log_event("workspace_initialized")
        vector_result = vectorstore.init_vectorstore(
            config["api_key"],
            config["base_url"],
            config["embedding_model"],
            config.get("embedding_base_url"),
            config.get("embedding_api_key"),
        )
        logging_service.log_event(
            "vectorstore_initialized",
            collection_name=vector_result.collection_name,
            indexed_count=vector_result.indexed_count,
            path=vector_result.path,
        )
        print(f"[向量存储] 初始化成功，已索引 {vector_result.indexed_count} 篇帖子。")
        vector_index_service.ensure_collection(
            collection_name=vector_result.collection_name,
            embedding_config_hash=vectorstore.current_embedding_config_hash() or "",
            embedding_model=config["embedding_model"],
            embedding_base_url=config.get("embedding_base_url") or config["base_url"],
        )
        reindexed_vector_docs = record_service.reindex_all_vector_docs()
        fixed_vector_docs = record_service.retry_pending_vector_docs()
        if reindexed_vector_docs or fixed_vector_docs:
            print(f"[向量存储] 已同步 {reindexed_vector_docs + fixed_vector_docs} 条向量任务。")
        sessions.run_memory_reconcile(client, model, trigger="cli_startup")
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
            sessions.run_memory_reconcile(client, model, trigger="cli_exit")
            print("再见！\n")
            break

        if not user_input:
            continue
        chat_handled, todos, quit_requested = commands.handle_chat_command(user_input, client, model, todos)
        if quit_requested:
            sessions.run_memory_reconcile(client, model, trigger="cli_exit")
            print("再见！\n")
            break
        if chat_handled:
            continue
        comment_handled, todos, quit_requested = commands.handle_comment_command(user_input, client, model, todos)
        if quit_requested:
            sessions.run_memory_reconcile(client, model, trigger="cli_exit")
            print("再见！\n")
            break
        if comment_handled:
            continue
        if commands.handle_tool_command(user_input):
            todos = todo_service.load_todos() if tool_config_service.is_tool_enabled("todo") else []
            continue
        if commands.handle_soul_command(user_input):
            continue

        rewritten_query = query_rewriter.rewrite_query(
            client,
            model,
            user_input,
            "public_post",
            trace_context={"channel": "public_post"},
        )
        logging_service.log_event(
            "query_rewrite_result",
            channel="public_post",
            raw_query=rewritten_query.raw_query,
            semantic_query=rewritten_query.semantic_query,
            keywords=rewritten_query.keywords,
            used_rewrite=rewritten_query.used_rewrite,
            keyword_count=len(rewritten_query.keywords),
            semantic_query_length=len(rewritten_query.semantic_query),
            raw_query_length=len(rewritten_query.raw_query),
            rewrite_skipped_by_gate=rewritten_query.rewrite_skipped_by_gate,
        )
        relevant_ids = retrieval.hybrid_search(
            user_input,
            k=3,
            semantic_query=rewritten_query.semantic_query,
            fts_keywords=rewritten_query.keywords,
            trace_context={"channel": "public_post"},
        )

        print("\n[TraceLog 正在思考...]\n")
        built_context = context_builder.build_context(
            relevant_post_ids=relevant_ids,
            query=user_input,
            fts_keywords=rewritten_query.keywords,
            trace_context={"channel": "public_post"},
        )

        post_id = record_service.save_post(user_input)
        logging_service.log_event("post_saved", post_id=post_id, content_length=len(user_input))
        sessions.run_todo_tool_for_post(post_id, client, model)

        results = reply_service.fanout(post_id, user_input, client, model, built_context)
        if not results:
            print("[TraceLog] 当前没有启用 SOUL，未生成评论。\n")
            sessions.run_memory_reconcile(client, model, trigger="cli_post")
            continue

        for result in results:
            if result.ok:
                print(f"[{result.soul_name}] {result.reply}\n")
            else:
                print(f"[{result.soul_name}] {result.reply}（{result.error}）\n")

        sessions.run_memory_reconcile(client, model, trigger="cli_post")

        if not any(result.ok for result in results):
            print("[TraceLog] 所有 SOUL 本次都回复失败，post 已保存，可稍后重试。\n")
            continue

        if tool_config_service.is_tool_enabled("todo"):
            todos = todo_service.load_todos()
