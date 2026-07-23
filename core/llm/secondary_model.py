"""副模型（secondary utility model）的安装与解析。

主模型承担人格回复、私聊与记忆整理；副模型是可选的轻量档位，承接
搜索门控、查询改写、目标抽取这类小输出工具调用。未配置副模型时
``resolve()`` 原样返回主模型，所有工具调用自动回落。
"""

from __future__ import annotations

from typing import Any, Callable

from core.llm.types import LLMClient

_client: LLMClient | None = None
_model: str | None = None


def effective_config(config: dict[str, Any] | None) -> dict[str, str | None] | None:
    """Resolved secondary settings, or None when no secondary model is set.

    api_key/base_url fall back to the main model's credentials when unset."""
    source = config if isinstance(config, dict) else {}
    model = _clean(source.get("secondary_model"))
    if model is None:
        return None
    return {
        "model": model,
        "api_key": _clean(source.get("secondary_api_key")) or _clean(source.get("api_key")),
        "base_url": _clean(source.get("secondary_base_url")) or _clean(source.get("base_url")),
    }


def install_from_config(
    config: dict[str, Any] | None,
    *,
    main_client: LLMClient | None,
    client_factory: Callable[[str, str], LLMClient],
) -> None:
    """Install the process-wide secondary client; call at startup and reload.

    Reuses ``main_client`` when the secondary shares the main credentials, so
    the common "same key, faster model" setup costs no extra client."""
    settings = effective_config(config)
    if settings is None:
        reset()
        return
    source = config if isinstance(config, dict) else {}
    same_credentials = (
        settings["api_key"] == _clean(source.get("api_key"))
        and settings["base_url"] == _clean(source.get("base_url"))
    )
    if same_credentials:
        if main_client is None:
            reset()
        else:
            configure(main_client, settings["model"])
        return
    if not settings["api_key"] or not settings["base_url"]:
        reset()
        return
    configure(client_factory(settings["api_key"], settings["base_url"]), settings["model"])


def configure(client: LLMClient | None, model: str | None) -> None:
    global _client, _model
    _client = client
    _model = _clean(model)


def reset() -> None:
    configure(None, None)


def is_configured() -> bool:
    return _client is not None and bool(_model)


def resolve(
    client: LLMClient | None, model: str | None
) -> tuple[LLMClient | None, str | None]:
    """The (client, model) a utility call should use — secondary when installed."""
    if _client is not None and _model:
        return _client, _model
    return client, model


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
