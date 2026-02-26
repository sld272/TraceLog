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
    加载配置文件。若不存在，引导用户首次输入 API Key 和模型名并保存。
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
    base_url_display = config.get("base_url", "https://api.openai.com/v1")
    print(f"模型: {model}  |  Base URL: {base_url_display}\n")

    profile = memory.load_profile()
    entry_count = profile["meta"].get("entry_count", 0)
    if entry_count > 0:
        print(f"[记忆] 已加载画像，共 {entry_count} 条日记记录。\n")

    while True:
        try:
            user_input = input("你: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n再见！")
            break

        if not user_input:
            continue

        print("\n[TraceLog 正在思考...]\n")
        result = router.call_router(
            user_input, client, model,
            context=memory.build_context_summary(profile),
        )

        if result is None:
            print("[TraceLog] 本次解析失败，请重试。\n")
            continue

        print(f"TraceLog: {result['reply']}\n")

        # 合并画像、更新简介、持久化
        profile = memory.merge_profile(profile, result["extracted_data"])
        print("[记忆] 正在更新画像简介...")
        profile["portrait"] = router.update_portrait(profile, client, model)
        memory.save_profile(profile)
        print("[记忆] 画像已保存。\n")

        # 调试输出：打印提取的结构化数据
        print("-" * 40)
        print("[调试] extracted_data:")
        print(json.dumps(result["extracted_data"], ensure_ascii=False, indent=2))
        print("-" * 40 + "\n")


if __name__ == "__main__":
    main()
