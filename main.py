"""
TraceLog 拾迹 — 个人成长 AI 伴侣
"""

import json
import os
import getpass
from openai import OpenAI
import router
import memory

CONFIG_FILE = "config.json"


def load_config() -> dict:
    """
    加载配置文件。若不存在，引导用户首次配置和模型并保存。
    """
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    print("=" * 50)
    print("  欢迎使用 TraceLog 拾迹！首次运行需要配置。")
    print("=" * 50)
    print("请前往你的 API 提供商获取 API Key。")

    api_key = getpass.getpass("请输入 API Key（输入时不显示）: ").strip()
    if not api_key:
        raise ValueError("API Key 不能为空，请重新运行程序并输入有效的 API Key。")

    base_url = input("请输入 API Base URL（直接回车使用 OpenAI 官方地址）: ").strip()
    if not base_url:
        base_url = "https://api.openai.com/v1"

    model = input("请输入模型名称（直接回车使用默认 gpt-4o-mini）: ").strip()
    if not model:
        model = "gpt-4o-mini"

    config = {"api_key": api_key, "base_url": base_url, "model": model}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"\n配置已保存到 {CONFIG_FILE} 。\n")
    return config


def main():
    print("\n" + "=" * 50)
    print("  TraceLog 拾迹 ✦ 个人成长 AI 伴侣")
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

    memory.init_workspace()
    todos = memory.load_todos()

    while True:
        try:
            user_input = input("你: ").strip()
            if user_input.lower() == "/quit":
                raise KeyboardInterrupt
        except (KeyboardInterrupt, EOFError):
            print("\n\n[记忆] 正在静默整理今日记忆...")
            new_profile = router.flush_profile(
                client, model,
                old_profile=memory.read_profile(),
                recent_posts=memory.read_recent_posts(),
            )
            if new_profile and len(new_profile) > 50:
                memory.write_profile(new_profile)
                print("[记忆] 画像已更新。")
            else:
                print("[记忆] 画像生成异常或过短，已放弃本次覆盖，保护旧数据。")
            print("再见！\n")
            break

        if not user_input:
            continue

        # 1. 保存帖子
        memory.save_post(user_input)

        # 2. 处理帖子回复
        print("\n[TraceLog 正在思考...]\n")
        context = memory.build_context()
        result = router.call_post_reply(user_input, client, model, context)

        if result is None:
            print("[TraceLog] 本次解析失败，请重试。\n")
            continue

        # 3. 打印回复
        print(f"TraceLog: {result['reply']}\n")

        # 4. 更新待办
        to_upsert = result.get("todos_to_upsert", [])
        to_delete = result.get("todos_to_delete", [])
        if to_upsert or to_delete:
            todos = memory.upsert_todos(todos, to_upsert, to_delete)
            memory.save_todos(todos)
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
