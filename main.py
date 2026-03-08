"""
TraceLog 拾迹 — 个人成长 AI 伴侣
"""

import json
import os
import getpass
from openai import OpenAI
import router
import memory
import vectorstore

CONFIG_FILE = "config.json"


def _is_valid_profile(content: str | None) -> bool:
    """校验画像文本是否具备最小有效性，避免仅靠长度阈值误判。"""
    if not content:
        return False
    text = content.strip()
    if len(text) < 10:
        return False
    has_markdown_structure = ("##" in text) or ("- " in text)
    return has_markdown_structure


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

    memory.init_workspace()
    vectorstore.init_vectorstore(
        config["api_key"],
        config["base_url"],
        config["embedding_model"],
        config.get("embedding_base_url"),
        config.get("embedding_api_key"),
    )
    todos = memory.load_todos()

    while True:
        try:
            user_input = input("你: ").strip()
            if user_input.lower() == "/quit":
                raise KeyboardInterrupt
        except (KeyboardInterrupt, EOFError):
            print("\n\n[记忆] 正在静默整理今日记忆，请稍候（请勿再次终止）...")
            try:
                new_profile = router.flush_profile(
                    client, model,
                    old_profile=memory.read_profile(),
                    recent_posts=memory.read_recent_posts(),
                )
                if _is_valid_profile(new_profile):
                    memory.write_profile(new_profile.strip())
                    print("[记忆] 画像已更新。")
                else:
                    print("[记忆] 画像内容无效或过短，已放弃本次覆盖，保护旧数据。")
            except KeyboardInterrupt:
                print("\n[警告] 记忆整理被强制中断，已保留旧画像。")
            except Exception as e:
                print(f"[记忆] 画像整理失败：{e}，已保留旧画像。")
            print("再见！\n")
            break

        if not user_input:
            continue

        # 1. 先检索历史（不包含当前输入），避免“自己搜自己”
        relevant_ids = vectorstore.search_relevant_posts(user_input, n_results=3)

        # 2. 基于历史组装上下文，避免当前输入在上下文中重复出现
        print("\n[TraceLog 正在思考...]\n")
        context = memory.build_context(relevant_post_ids=relevant_ids)

        # 3. 落盘与索引当前输入，确保即使后续 LLM 失败也不丢用户数据
        post_id = memory.save_post(user_input)
        vectorstore.index_post(post_id, user_input)

        # 4. 调用 LLM
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
