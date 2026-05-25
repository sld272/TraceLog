"""
TraceLog 拾迹 — 个人成长 AI 伴侣
"""

import json
import os
import getpass
from typing import cast
from openai import OpenAI
from core import chat_service
from core import context_builder
from core import reflector
from core import record_service
from core import reply_service
from core import retrieval
from core import soul_service
from core import todo_service
import memory
import vectorstore

CONFIG_FILE = "config.json"


def _run_deep_reflection_on_exit(client: OpenAI, model: str) -> None:
    print("\n\n[反思] 正在触发一次深反思，请稍候（请勿再次终止）...")
    try:
        result = reflector.trigger_global_deep_reflection(client, model, trigger="cli_exit")
        if result is None:
            print("[反思] 没有新的公开记录，已跳过本次深反思。")
        else:
            print(f"[反思] 深反思已保存（id={result.id}，覆盖 {len(result.related_post_ids)} 条记录）。")
    except KeyboardInterrupt:
        print("\n[警告] 深反思被强制中断，已有数据保持不变。")
    except Exception as e:
        print(f"[反思] 深反思失败：{e}，已有数据保持不变。")
    print("再见！\n")


def _run_light_reflection_for_post(post_id: str, client: OpenAI, model: str) -> None:
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


def _handle_chat_command(
    user_input: str,
    client: OpenAI,
    model: str,
    todos: list,
) -> tuple[bool, list, bool]:
    """Handle /chat commands. Returns handled, todos, quit_requested."""
    if user_input == "/chat list":
        _print_chat_threads()
        return True, todos, False
    if user_input != "/chat" and not user_input.startswith("/chat "):
        return False, todos, False

    parts = user_input.split(maxsplit=1)
    if len(parts) == 1 or not parts[1].strip():
        _print_chat_help()
        return True, todos, False

    soul_name = parts[1].strip()
    if soul_name == "list":
        _print_chat_threads()
        return True, todos, False

    try:
        thread = chat_service.get_or_create_thread(soul_name)
    except ValueError as exc:
        print(f"[私聊] {exc}\n")
        return True, todos, False

    updated_todos, quit_requested = _run_chat_session(thread, client, model, todos)
    return True, updated_todos, quit_requested


def _run_chat_session(
    thread: chat_service.ChatThread,
    client: OpenAI,
    model: str,
    todos: list,
) -> tuple[list, bool]:
    print(f"\n[私聊] 已进入与 {thread.soul_name} 的私聊。输入 /back 返回发帖模式，/quit 退出。\n")
    current_todos = todos
    while True:
        try:
            raw_input = cast(str, input(f"[{thread.soul_name}] 你: "))
        except EOFError:
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
            if result.assistant_message_id is not None:
                try:
                    current_todos = todo_service.apply_chat_todos(result, result.assistant_message_id)
                    if result.todos_to_upsert or result.todos_to_delete:
                        print(f"[记忆] 待办已更新，当前 {len(current_todos)} 条。\n")
                except Exception as exc:
                    print(f"[记忆] 私聊待办合流失败：{exc}\n")
        else:
            print(f"[{result.soul_name}] {result.reply}（{result.error}）\n")


def _print_chat_threads() -> None:
    threads = chat_service.list_chat_threads()
    if not threads:
        print("[私聊] 当前没有私聊线程。可用 /chat <soul> 创建。\n")
        return
    print("\n[私聊线程]")
    for thread in threads:
        last = thread.last_message_at or thread.updated_at or thread.created_at
        print(f"{thread.id}. {thread.soul_name} - {thread.title or '未命名'}（last={last:.0f}）")
    print()


def _print_chat_help() -> None:
    print(
        "[私聊] 可用命令：\n"
        "  /chat list\n"
        "  /chat <soul>\n"
        "私聊中输入 /back 返回发帖模式，/quit 退出。\n"
    )


def _handle_soul_command(user_input: str) -> bool:
    """Handle SOUL management commands. Returns True if input was a command."""
    if user_input == "/souls":
        _print_souls()
        return True
    if user_input != "/soul" and not user_input.startswith("/soul "):
        return False

    parts = user_input.split(maxsplit=3)
    if len(parts) == 1:
        _print_soul_help()
        return True

    action = parts[1]
    try:
        if action == "resync":
            soul_service.sync_souls()
            print("[SOUL] 已重新扫描 SOUL 库。\n")
            _print_souls()
        elif action == "enable" and len(parts) >= 3:
            record = soul_service.enable_soul(parts[2])
            print(f"[SOUL] 已启用：{record.name}\n")
        elif action == "disable" and len(parts) >= 3:
            record = soul_service.disable_soul(parts[2])
            print(f"[SOUL] 已禁用：{record.name}\n")
        elif action == "reorder" and len(parts) >= 3:
            names = parts[2:]
            if len(parts) == 4:
                names = [parts[2], *parts[3].split()]
            soul_service.reorder_souls(names)
            print("[SOUL] 排序已更新。\n")
            _print_souls()
        elif action == "create" and len(parts) >= 3:
            name = parts[2]
            description = parts[3] if len(parts) >= 4 else None
            record = soul_service.create_soul(name, description=description)
            print(f"[SOUL] 已创建：{record.name}。可编辑 workspace/{record.file_path} 调整人格。\n")
        else:
            _print_soul_help()
    except ValueError as exc:
        print(f"[SOUL] {exc}\n")
    except OSError as exc:
        print(f"[SOUL] 文件操作失败：{exc}\n")
    return True


def _print_souls() -> None:
    records = soul_service.list_souls()
    if not records:
        print("[SOUL] 当前没有 SOUL。可用 /soul resync 初始化。\n")
        return
    print("\n[SOUL 库]")
    for record in records:
        status = "启用" if record.enabled else "禁用"
        persona_status = "" if record.persona_exists else "（人格文件缺失）"
        memory_status = "" if record.memory_exists else "（记忆文件缺失）"
        description = f" - {record.description}" if record.description else ""
        print(
            f"{record.sort_order:02d}. [{status}] {record.name}"
            f"{description}{persona_status}{memory_status}"
        )
    print()


def _print_soul_help() -> None:
    print(
        "[SOUL] 可用命令：\n"
        "  /souls\n"
        "  /soul create <name> [description]\n"
        "  /soul enable <name>\n"
        "  /soul disable <name>\n"
        "  /soul reorder <name1> <name2> ...\n"
        "  /soul resync\n"
    )


def load_config() -> dict:
    """
    加载配置文件。若不存在，引导用户首次配置和模型并保存。
    """
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

        required_keys = ("api_key", "base_url", "model", "embedding_model")
        missing = [k for k in required_keys if not config.get(k)]
        if not missing:
            config.setdefault("embedding_api_key", None)
            config.setdefault("embedding_base_url", None)
            return config

        print(f"[配置] 检测到配置不完整（缺少：{', '.join(missing)}），将重新配置。")
        os.remove(CONFIG_FILE)

    print("=" * 50)
    print("欢迎使用 TraceLog 拾迹！首次运行需要配置。")
    print("=" * 50)

    api_key = getpass.getpass("请输入 API Key（输入时不显示）: ").strip()
    if not api_key:
        raise ValueError("API Key 不能为空，请重新运行程序并输入有效的 API Key。")

    base_url = input("请输入 API Base URL（直接回车使用 OpenAI 官方地址）: ").strip()
    if not base_url:
        base_url = "https://api.openai.com/v1"

    model = input("请输入模型名称（直接回车使用默认 gpt-4o-mini）: ").strip()
    if not model:
        model = "gpt-4o-mini"

    print("\n接下来配置向量 Embedding（用于语义记忆检索）：")
    emb_model = input("请输入 Embedding 模型名称（直接回车使用 text-embedding-3-small）: ").strip()
    embedding_model = emb_model or "text-embedding-3-small"

    use_sep = input("是否为 Embedding 单独配置 API Key 和 Base URL？[y/n]（回车跳过复用主配置）: ").strip().lower()
    embedding_api_key = None
    embedding_base_url = None
    if use_sep and use_sep[0] == "y":
        emb_key = getpass.getpass("请输入 Embedding API Key（回车跳过复用主 Key）: ").strip()
        embedding_api_key = emb_key or None
        emb_url = input("请输入 Embedding Base URL（回车跳过复用主 URL）: ").strip()
        embedding_base_url = emb_url or None

    config = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "embedding_model": embedding_model,
        "embedding_api_key": embedding_api_key,
        "embedding_base_url": embedding_base_url,
    }
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)

    print(f"\n配置已保存到 {CONFIG_FILE} 。\n")
    return config


def main():
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
        memory.init_workspace()
        vectorstore.init_vectorstore(
            config["api_key"],
            config["base_url"],
            config["embedding_model"],
            config.get("embedding_base_url"),
            config.get("embedding_api_key"),
        )
        fixed_embeddings = record_service.retry_pending_embeddings()
        if fixed_embeddings:
            print(f"[向量存储] 已补齐 {fixed_embeddings} 条待索引帖子。")
        fixed_reflections = reflector.retry_pending_light_reflections(client, model)
        if fixed_reflections:
            print(f"[反思] 已补跑 {fixed_reflections} 条轻反思。")
        todos = memory.load_todos()
    except KeyboardInterrupt:
        print("\n[启动] 初始化被中断，已尽量回滚数据库事务。请重新运行。")
        return

    while True:
        try:
            raw_input = cast(str, input("你: "))
            user_input = raw_input.strip()
            if user_input.lower() == "/quit":
                raise KeyboardInterrupt
        except (KeyboardInterrupt, EOFError):
            _run_deep_reflection_on_exit(client, model)
            break

        if not user_input:
            continue
        chat_handled, todos, quit_requested = _handle_chat_command(user_input, client, model, todos)
        if quit_requested:
            _run_deep_reflection_on_exit(client, model)
            break
        if chat_handled:
            continue
        if _handle_soul_command(user_input):
            continue

        # 1. 先检索历史（不包含当前输入），避免“自己搜自己”
        relevant_ids = retrieval.hybrid_search(user_input, k=3)

        # 2. 基于历史组装上下文，避免当前输入在上下文中重复出现
        print("\n[TraceLog 正在思考...]\n")
        built_context = context_builder.build_context(relevant_post_ids=relevant_ids)

        # 3. 落盘与索引当前输入，确保即使后续 LLM 失败也不丢用户数据
        post_id = record_service.save_post(user_input)

        # 4. 并发调用启用 SOUL，写入 comments
        results = reply_service.fanout(post_id, user_input, client, model, built_context)
        if not results:
            print("[TraceLog] 当前没有启用 SOUL，未生成评论。\n")
            _run_light_reflection_for_post(post_id, client, model)
            continue

        # 5. 打印多 SOUL 评论
        for result in results:
            if result.ok:
                print(f"[{result.soul_name}] {result.reply}\n")
            else:
                print(f"[{result.soul_name}] {result.reply}（{result.error}）\n")

        _run_light_reflection_for_post(post_id, client, model)

        if not any(result.ok for result in results):
            print("[TraceLog] 所有 SOUL 本次都回复失败，post 已保存，可稍后重试。\n")
            continue

        # 6. 合并成功 SOUL 抽取的待办
        to_upsert, to_delete = todo_service.merge_reply_todos(results)
        if to_upsert or to_delete:
            todos = todo_service.apply_reply_todos(todos, results)
            print(f"[记忆] 待办已更新，当前 {len(todos)} 条。\n")

        # 调试输出
        if to_upsert or to_delete:
            print("-" * 40)
            if to_upsert:
                print("[调试] todos_to_upsert:")
                print(json.dumps(to_upsert, ensure_ascii=False, indent=2))
            if to_delete:
                print("[调试] todos_to_delete:")
                print(json.dumps(to_delete, ensure_ascii=False, indent=2))
            print("-" * 40 + "\n")


if __name__ == "__main__":
    main()
