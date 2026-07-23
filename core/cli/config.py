"""CLI configuration loading and first-run setup."""

from __future__ import annotations

import getpass
import json
import os

from core import file_security
from core.cli_input import read_cli_input
from core.logging_service import default_config as default_logging_config
from core.logging_service import normalize_config as normalize_logging_settings
from core.paths import CONFIG_FILE as CONFIG_PATH

CONFIG_FILE = str(CONFIG_PATH)
DEFAULT_VISION_CONFIG = {
    "enabled": False,
    "model": None,
    "api_key": None,
    "base_url": None,
}
DEFAULT_WEB_SEARCH_CONFIG = {
    "enabled": False,
    "provider": "duckduckgo",
    "tavily_api_key": None,
    "max_results": 5,
    "timeout_s": 8,
    "cache_ttl_s": 1800,
}
WEB_SEARCH_PROVIDERS = {"tavily", "duckduckgo"}


def load_config() -> dict:
    """Load config.json or guide the user through first-run setup."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

        required_keys = ("api_key", "base_url", "model", "embedding_model")
        missing = [key for key in required_keys if not config.get(key)]
        if not missing:
            config.setdefault("embedding_api_key", None)
            config.setdefault("embedding_base_url", None)
            config.setdefault("secondary_model", None)
            config.setdefault("secondary_api_key", None)
            config.setdefault("secondary_base_url", None)
            config["logging"] = _normalize_logging_config(config.get("logging"))
            config["vision"] = normalize_vision_config(config.get("vision"))
            config["web_search"] = normalize_web_search_config(config.get("web_search"))
            return config

        print(f"[配置] 检测到配置不完整（缺少：{', '.join(missing)}），将重新配置。")
        os.remove(CONFIG_FILE)

    print("=" * 50)
    print("欢迎使用 TraceLog 拾迹！首次运行需要配置。")
    print("=" * 50)

    api_key = getpass.getpass("请输入 API Key（输入时不显示）: ").strip()
    if not api_key:
        raise ValueError("API Key 不能为空，请重新运行程序并输入有效的 API Key。")

    base_url = read_cli_input("请输入 API Base URL（直接回车使用 OpenAI 官方地址）: ").strip()
    if not base_url:
        base_url = "https://api.openai.com/v1"

    model = read_cli_input("请输入模型名称（直接回车使用默认 gpt-4o-mini）: ").strip()
    if not model:
        model = "gpt-4o-mini"

    print("\n接下来配置向量 Embedding（用于语义记忆检索）：")
    emb_model = read_cli_input("请输入 Embedding 模型名称（直接回车使用 text-embedding-3-small）: ").strip()
    embedding_model = emb_model or "text-embedding-3-small"

    use_sep = read_cli_input("是否为 Embedding 单独配置 API Key 和 Base URL？[y/n]（回车跳过复用主配置）: ").strip().lower()
    embedding_api_key = None
    embedding_base_url = None
    if use_sep and use_sep[0] == "y":
        emb_key = getpass.getpass("请输入 Embedding API Key（回车跳过复用主 Key）: ").strip()
        embedding_api_key = emb_key or None
        emb_url = read_cli_input("请输入 Embedding Base URL（回车跳过复用主 URL）: ").strip()
        embedding_base_url = emb_url or None

    config = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "embedding_model": embedding_model,
        "embedding_api_key": embedding_api_key,
        "embedding_base_url": embedding_base_url,
        "secondary_model": None,
        "secondary_api_key": None,
        "secondary_base_url": None,
        "logging": default_logging_config(),
        "vision": default_vision_config(),
        "web_search": default_web_search_config(),
    }
    tmp = CONFIG_FILE + ".tmp"
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    file_security.make_private(CONFIG_FILE)

    print(f"\n配置已保存到 {CONFIG_FILE} 。\n")
    return config


def _normalize_logging_config(value) -> dict:
    return normalize_logging_settings(value)


def default_vision_config() -> dict:
    return dict(DEFAULT_VISION_CONFIG)


def default_web_search_config() -> dict:
    return dict(DEFAULT_WEB_SEARCH_CONFIG)


def normalize_vision_config(value) -> dict:
    raw = value if isinstance(value, dict) else {}
    merged = default_vision_config()
    merged.update(
        {
            key: _clean_optional(raw.get(key)) if key != "enabled" else bool(raw.get(key))
            for key in DEFAULT_VISION_CONFIG
            if key in raw
        }
    )
    merged["enabled"] = bool(merged.get("enabled"))
    if not merged.get("enabled"):
        merged["api_key"] = _clean_optional(merged.get("api_key"))
        merged["base_url"] = _clean_optional(merged.get("base_url"))
        merged["model"] = _clean_optional(merged.get("model"))
        return merged
    merged["model"] = _clean_optional(merged.get("model"))
    merged["api_key"] = _clean_optional(merged.get("api_key"))
    merged["base_url"] = _clean_optional(merged.get("base_url"))
    return merged


def normalize_web_search_config(value) -> dict:
    raw = value if isinstance(value, dict) else {}
    merged = default_web_search_config()
    merged.update(
        {
            key: _clean_optional(raw.get(key))
            if key in {"provider", "tavily_api_key"}
            else raw.get(key)
            for key in DEFAULT_WEB_SEARCH_CONFIG
            if key in raw
        }
    )
    merged["enabled"] = bool(merged.get("enabled"))
    provider = str(merged.get("provider") or "duckduckgo").strip().lower()
    merged["provider"] = provider if provider in WEB_SEARCH_PROVIDERS else "duckduckgo"
    merged["tavily_api_key"] = _clean_optional(merged.get("tavily_api_key"))
    merged["max_results"] = _clamp_int(merged.get("max_results"), 5, 1, 8)
    merged["timeout_s"] = _clamp_int(merged.get("timeout_s"), 8, 3, 20)
    merged["cache_ttl_s"] = _clamp_int(merged.get("cache_ttl_s"), 1800, 0, 86400)
    return merged


def _clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _clean_optional(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
