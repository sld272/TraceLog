"""CLI command parsing and display helpers."""

from __future__ import annotations

from core import chat_service, comment_service, soul_service, tool_config_service
from core.cli import sessions
from core.llm.types import LLMClient


def handle_chat_command(
    user_input: str,
    client: LLMClient | None,
    model: str,
    todos: list,
) -> tuple[bool, list, bool]:
    """Handle /chat commands. Returns handled, todos, quit_requested."""
    if user_input == "/chat list":
        print_chat_threads()
        return True, todos, False
    if user_input != "/chat" and not user_input.startswith("/chat "):
        return False, todos, False

    parts = user_input.split(maxsplit=1)
    if len(parts) == 1 or not parts[1].strip():
        print_chat_help()
        return True, todos, False

    soul_name = parts[1].strip()
    if soul_name == "list":
        print_chat_threads()
        return True, todos, False

    try:
        thread = chat_service.get_or_create_thread(soul_name)
    except ValueError as exc:
        print(f"[私聊] {exc}\n")
        return True, todos, False

    if client is None:
        raise ValueError("LLM client is required for chat commands")
    updated_todos, quit_requested = sessions.run_chat_session(thread, client, model, todos)
    return True, updated_todos, quit_requested


def handle_comment_command(
    user_input: str,
    client: LLMClient | None,
    model: str,
    todos: list,
) -> tuple[bool, list, bool]:
    """Handle /comment commands. Returns handled, todos, quit_requested."""
    if user_input != "/comment" and not user_input.startswith("/comment "):
        return False, todos, False

    parts = user_input.split(maxsplit=2)
    if len(parts) < 3:
        print_comment_help()
        return True, todos, False

    post_id = parts[1].strip()
    soul_name = parts[2].strip()
    if not post_id or not soul_name:
        print_comment_help()
        return True, todos, False

    try:
        thread = comment_service.get_or_create_thread(post_id, soul_name)
    except ValueError as exc:
        print(f"[评论] {exc}\n")
        return True, todos, False

    if client is None:
        raise ValueError("LLM client is required for comment commands")
    updated_todos, quit_requested = sessions.run_comment_session(thread, client, model, todos)
    return True, updated_todos, quit_requested


def handle_soul_command(user_input: str) -> bool:
    """Handle SOUL management commands. Returns True if input was a command."""
    if user_input == "/souls":
        print_souls()
        return True
    if user_input != "/soul" and not user_input.startswith("/soul "):
        return False

    parts = user_input.split(maxsplit=3)
    if len(parts) == 1:
        print_soul_help()
        return True

    action = parts[1]
    try:
        if action == "resync":
            soul_service.sync_souls()
            print("[SOUL] 已重新扫描 SOUL 库。\n")
            print_souls()
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
            print_souls()
        elif action == "create" and len(parts) >= 3:
            name = parts[2]
            description = parts[3] if len(parts) >= 4 else None
            record = soul_service.create_soul(name, description=description)
            print(f"[SOUL] 已创建：{record.name}。可编辑 workspace/{record.file_path} 调整人格。\n")
        else:
            print_soul_help()
    except ValueError as exc:
        print(f"[SOUL] {exc}\n")
    except OSError as exc:
        print(f"[SOUL] 文件操作失败：{exc}\n")
    return True


def handle_tool_command(user_input: str) -> bool:
    """Handle optional tool commands. Returns True if input was a command."""
    if user_input == "/tools":
        print_tools()
        return True
    if user_input != "/tool" and not user_input.startswith("/tool "):
        return False

    parts = user_input.split(maxsplit=2)
    if len(parts) < 3:
        print_tool_help()
        return True

    name = parts[1].strip()
    action = parts[2].strip().lower()
    try:
        if action in {"on", "enable", "enabled", "开启"}:
            tool_config_service.set_tool_enabled(name, True)
            print(f"[工具] 已开启：{name}\n")
        elif action in {"off", "disable", "disabled", "关闭"}:
            tool_config_service.set_tool_enabled(name, False)
            print(f"[工具] 已关闭：{name}\n")
        else:
            print_tool_help()
    except ValueError as exc:
        print(f"[工具] {exc}\n")
    return True


def print_comment_help() -> None:
    print(
        "[评论] 可用命令：\n"
        "  /comment <post_id> <soul>\n"
        "评论线程中输入 /back 返回发帖模式，/quit 退出。\n"
    )


def print_chat_threads() -> None:
    threads = chat_service.list_chat_threads()
    if not threads:
        print("[私聊] 当前没有私聊线程。可用 /chat <soul> 创建。\n")
        return
    print("\n[私聊线程]")
    for thread in threads:
        last = thread.last_message_at or thread.updated_at or thread.created_at
        print(f"{thread.id}. {thread.soul_name} - {thread.title or '未命名'}（last={last:.0f}）")
    print()


def print_chat_help() -> None:
    print(
        "[私聊] 可用命令：\n"
        "  /chat list\n"
        "  /chat <soul>\n"
        "私聊中输入 /back 返回发帖模式，/quit 退出。\n"
    )


def print_tools() -> None:
    status = "开启" if tool_config_service.is_tool_enabled("todo") else "关闭"
    print(f"\n[工具]\n  todo: {status}\n")


def print_tool_help() -> None:
    print(
        "[工具] 可用命令：\n"
        "  /tools\n"
        "  /tool todo on\n"
        "  /tool todo off\n"
    )


def print_souls() -> None:
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


def print_soul_help() -> None:
    print(
        "[SOUL] 可用命令：\n"
        "  /souls\n"
        "  /soul create <name> [description]\n"
        "  /soul enable <name>\n"
        "  /soul disable <name>\n"
        "  /soul reorder <name1> <name2> ...\n"
        "  /soul resync\n"
    )
