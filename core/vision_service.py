"""Image understanding cache and prompt enrichment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import attachment_service, db, logging_service
from core.cli.config import CONFIG_FILE, normalize_vision_config
from core.llm.common import call_json_completion

PROMPT_VERSION = "vision-summary-v1"
VISION_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class VisionSummary:
    attachment_id: str
    status: str
    description: str
    visible_text: list[str]
    uncertainties: list[str]
    error: str | None = None


def configured_status(config: dict | None = None) -> dict[str, Any]:
    settings = _effective_config(config or _load_config())
    return {
        "enabled": bool(settings["enabled"]),
        "configured": _is_ready(settings),
        "model": settings.get("model"),
        "base_url": settings.get("base_url"),
        "has_api_key": bool(settings.get("api_key")),
        "api_key_masked": _mask_secret(settings.get("api_key")),
        "prompt_version": PROMPT_VERSION,
        "timeout_s": VISION_TIMEOUT_SECONDS,
    }


def content_for_llm(content: str, attachments: list[attachment_service.Attachment]) -> str:
    summaries = describe_attachments(attachments)
    if summaries:
        return content_with_summaries(content, attachments, summaries)
    if not attachments:
        return content.strip()
    status = configured_status()
    if status["enabled"] and not status["configured"]:
        notice = f"用户附带了 {len(attachments)} 张图片，但识图配置未完成。不要描述、推断或声称看到了图片内容。"
        return _join_content_and_notice(content, notice)
    return attachment_service.content_for_llm(content, len(attachments))


def content_with_cached_summaries(content: str, attachments: list[attachment_service.Attachment]) -> str:
    summaries = cached_summaries_for_attachments(attachments)
    if summaries:
        return content_with_summaries(content, attachments, summaries)
    return attachment_service.content_for_llm(content, len(attachments))


def content_with_summaries(
    content: str,
    attachments: list[attachment_service.Attachment],
    summaries: list[VisionSummary],
) -> str:
    body = content.strip()
    context = format_summaries(summaries)
    if context:
        return f"{body}\n\n{context}" if body else context
    return attachment_service.content_for_llm(content, len(attachments))


def format_summaries(summaries: list[VisionSummary]) -> str:
    ok_items = [summary for summary in summaries if summary.status == "ok" and summary.description.strip()]
    if not ok_items:
        return ""
    parts = ["[图片理解摘要]"]
    for index, summary in enumerate(ok_items, start=1):
        lines = [f"- 图片 {index} ({summary.attachment_id}): {summary.description.strip()}"]
        if summary.visible_text:
            lines.append("  可见文字: " + "；".join(summary.visible_text))
        if summary.uncertainties:
            lines.append("  不确定: " + "；".join(summary.uncertainties))
        parts.append("\n".join(lines))
    return "\n".join(parts)


def describe_attachments(attachments: list[attachment_service.Attachment]) -> list[VisionSummary]:
    if not attachments:
        return []
    config = _effective_config(_load_config())
    if not _is_ready(config):
        return []

    cached = cached_summaries_for_attachments(attachments, config=config)
    cached_ids = {item.attachment_id for item in cached}
    missing = [attachment for attachment in attachments if attachment.id not in cached_ids]
    if not missing:
        return cached

    generated = _call_vision_llm(missing, config)
    return [*cached, *generated]


def cached_summaries_for_attachments(
    attachments: list[attachment_service.Attachment],
    *,
    config: dict | None = None,
) -> list[VisionSummary]:
    if not attachments:
        return []
    settings = _effective_config(config or _load_config())
    model = str(settings.get("model") or "")
    if not model:
        return []
    ids = [attachment.id for attachment in attachments]
    placeholders = ",".join("?" for _ in ids)
    rows = db.query_all(
        f"""
        SELECT attachment_id, status, description, visible_text, uncertainties, error
        FROM vision_cache
        WHERE attachment_id IN ({placeholders})
          AND model = ?
          AND prompt_version = ?
          AND status = 'ok'
        """,
        (*ids, model, PROMPT_VERSION),
    )
    by_id = {row["attachment_id"]: _summary_from_row(row) for row in rows}
    return [by_id[attachment.id] for attachment in attachments if attachment.id in by_id]


def cached_context_for_post(post_id: str) -> str:
    return format_summaries(cached_summaries_for_attachments(attachment_service.list_post_attachments(post_id)))


def cached_context_for_attachments(attachments: list[attachment_service.Attachment]) -> str:
    return format_summaries(cached_summaries_for_attachments(attachments))


def _call_vision_llm(attachments: list[attachment_service.Attachment], config: dict) -> list[VisionSummary]:
    try:
        from openai import OpenAI

        client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
        image_inputs = attachment_service.image_inputs_for_attachments(attachments)
        messages = _vision_messages(image_inputs)
        parsed = call_json_completion(
            client=client,
            model=str(config["model"]),
            operation="vision_summary",
            messages=messages,
            parser=_parse_vision_response,
            timeout=VISION_TIMEOUT_SECONDS,
            response_format={"type": "json_object"},
            trace_context={
                "attachment_ids": [attachment.id for attachment in attachments],
                "prompt_version": PROMPT_VERSION,
            },
        )
        if parsed is None:
            raise ValueError("vision response invalid")
        summaries = _summaries_from_parsed(parsed, attachments)
        for summary in summaries:
            _upsert_cache(summary, str(config["model"]))
        logging_service.log_event(
            "vision_summary_completed",
            attachment_ids=[summary.attachment_id for summary in summaries],
            model=config["model"],
            prompt_version=PROMPT_VERSION,
        )
        return summaries
    except Exception as exc:
        logging_service.log_event(
            "vision_summary_failed",
            level="WARNING",
            attachment_ids=[attachment.id for attachment in attachments],
            model=config.get("model"),
            error=str(exc),
        )
        failed = [
            VisionSummary(
                attachment_id=attachment.id,
                status="failed",
                description="",
                visible_text=[],
                uncertainties=[],
                error=str(exc),
            )
            for attachment in attachments
        ]
        for summary in failed:
            _upsert_cache(summary, str(config.get("model") or ""))
        return []


def _vision_messages(image_inputs: list[attachment_service.ImageInput]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "请理解这些用户上传的图片，输出严格 JSON。"
                "不要编造看不清的细节；如存在不确定性，写入 uncertainties。"
                "按 attachment_id 返回，每张图给出 description、visible_text、uncertainties。\n\n"
                "JSON 格式：{\"images\":[{\"attachment_id\":\"...\",\"description\":\"...\","
                "\"visible_text\":[\"...\"],\"uncertainties\":[\"...\"]}]}\n\n"
                "图片元数据：\n"
                + "\n".join(
                    f"- {item.attachment_id}: {item.mime_type}, {item.width}x{item.height}, {item.file_size} bytes"
                    for item in image_inputs
                )
            ),
        }
    ]
    for item in image_inputs:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": item.data_url,
                    "detail": "auto",
                },
            }
        )
    return [
        {
            "role": "system",
            "content": "你是 TraceLog 的图片理解模块，只做客观图像摘要，输出 JSON。",
        },
        {"role": "user", "content": content},
    ]


def _parse_vision_response(content: str | None) -> dict | None:
    try:
        data = json.loads((content or "").strip().removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("images"), list):
        return None
    return data


def _summaries_from_parsed(parsed: dict, attachments: list[attachment_service.Attachment]) -> list[VisionSummary]:
    allowed_ids = {attachment.id for attachment in attachments}
    summaries: list[VisionSummary] = []
    seen: set[str] = set()
    for item in parsed.get("images", []):
        if not isinstance(item, dict):
            continue
        attachment_id = str(item.get("attachment_id") or "").strip()
        if attachment_id not in allowed_ids or attachment_id in seen:
            continue
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        summaries.append(
            VisionSummary(
                attachment_id=attachment_id,
                status="ok",
                description=description,
                visible_text=_list_of_text(item.get("visible_text")),
                uncertainties=_list_of_text(item.get("uncertainties")),
            )
        )
        seen.add(attachment_id)
    missing = allowed_ids - seen
    for attachment_id in missing:
        summaries.append(
            VisionSummary(
                attachment_id=attachment_id,
                status="failed",
                description="",
                visible_text=[],
                uncertainties=[],
                error="vision response missing attachment",
            )
        )
    return summaries


def _summary_from_row(row) -> VisionSummary:
    return VisionSummary(
        attachment_id=row["attachment_id"],
        status=row["status"],
        description=row["description"] or "",
        visible_text=_json_list(row["visible_text"]),
        uncertainties=_json_list(row["uncertainties"]),
        error=row["error"],
    )


def _upsert_cache(summary: VisionSummary, model: str) -> None:
    now = db.now_ts()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO vision_cache(
                attachment_id, model, prompt_version, description, visible_text,
                uncertainties, status, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attachment_id, model, prompt_version) DO UPDATE SET
                description = excluded.description,
                visible_text = excluded.visible_text,
                uncertainties = excluded.uncertainties,
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                summary.attachment_id,
                model,
                PROMPT_VERSION,
                summary.description,
                json.dumps(summary.visible_text, ensure_ascii=False),
                json.dumps(summary.uncertainties, ensure_ascii=False),
                summary.status,
                summary.error,
                now,
                now,
            ),
        )


def _load_config() -> dict:
    try:
        data = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _effective_config(config: dict) -> dict:
    vision = normalize_vision_config(config.get("vision"))
    return {
        "enabled": vision.get("enabled"),
        "model": vision.get("model"),
        "api_key": vision.get("api_key") or config.get("api_key"),
        "base_url": vision.get("base_url") or config.get("base_url") or "https://api.openai.com/v1",
    }


def _is_ready(config: dict) -> bool:
    return bool(config.get("enabled") and config.get("model") and config.get("api_key") and config.get("base_url"))


def _join_content_and_notice(content: str, notice: str) -> str:
    body = content.strip()
    return f"{body}\n\n[{notice}]" if body else notice


def _json_list(value: Any) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return _list_of_text(decoded)


def _list_of_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _mask_secret(value: Any) -> str | None:
    text = str(value or "")
    if not text:
        return None
    if len(text) <= 8:
        return "••••"
    return f"{text[:4]}…{text[-4:]}"
