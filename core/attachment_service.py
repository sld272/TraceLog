"""Local image attachment storage and linking."""

from __future__ import annotations

import hashlib
import base64
import io
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from core import db

MAX_UPLOAD_IMAGE_BYTES = 50 * 1024 * 1024
MAX_STORED_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = MAX_STORED_IMAGE_BYTES
MAX_IMAGE_PIXELS = 60_000_000
MAX_IMAGE_SIDE = 12_000
MAX_COMPRESSED_IMAGE_SIDE = 3_840
MIN_COMPRESSED_IMAGE_SIDE = 512
MAX_ATTACHMENTS_PER_ENTITY = 9
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}
ACCEPTED_UPLOAD_MIME_TYPES = ALLOWED_MIME_TYPES | {"image/jpg", "image/pjpeg"}
GENERIC_UPLOAD_MIME_TYPES = {"application/octet-stream", "binary/octet-stream"}
ALLOWED_FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png")}
READ_FORMAT_ALIASES = {"JPEG": "JPEG", "JPG": "JPEG", "MPO": "JPEG", "PNG": "PNG"}
COMPRESSION_TOO_LARGE_MESSAGE = "图片压缩后体积仍超过5MB！"
JPEG_QUALITIES = (92, 88, 84, 80, 76, 72)


class UnsupportedImageFormatError(ValueError):
    def __init__(self, image_format: str | None) -> None:
        super().__init__("仅支持 JPEG 或 PNG 图片")
        self.image_format = image_format


@dataclass(frozen=True)
class Attachment:
    id: str
    file_path: str
    mime_type: str
    file_size: int
    width: int
    height: int
    sha256: str
    original_filename: str | None
    linked_at: float | None
    created_at: float
    url: str


@dataclass(frozen=True)
class ImageInput:
    attachment_id: str
    mime_type: str
    file_path: Path
    width: int
    height: int
    file_size: int
    data_url: str


def upload_image(file_bytes: bytes, *, content_type: str | None = None, filename: str | None = None) -> Attachment:
    """Validate, normalize, store one JPEG/PNG image, and return its metadata."""
    if len(file_bytes) > MAX_UPLOAD_IMAGE_BYTES:
        raise ValueError("图片不能超过 50MB")
    normalized_content_type = _normalize_content_type(content_type)
    if normalized_content_type and not _is_allowed_upload_content_type(normalized_content_type):
        raise ValueError("仅支持 JPEG 或 PNG 图片")

    image, image_format = _open_image(file_bytes)

    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("图片像素尺寸过大")

    normalized, stored_format, width, height = _encode_stored_image(image, image_format)
    if len(normalized) > MAX_STORED_IMAGE_BYTES:
        raise ValueError(COMPRESSION_TOO_LARGE_MESSAGE)
    mime_type, ext = ALLOWED_FORMATS[stored_format]

    attachment_id = _new_attachment_id()
    created_at = db.now_ts()
    relative_path = _relative_image_path(attachment_id, ext, created_at)
    target = db.WORKSPACE_DIR / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(target, normalized)

    sha256 = hashlib.sha256(normalized).hexdigest()
    db.execute(
        """
        INSERT INTO attachments(id, file_path, mime_type, file_size, width, height, sha256, original_filename, linked_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            attachment_id,
            relative_path.as_posix(),
            mime_type,
            len(normalized),
            width,
            height,
            sha256,
            _clean_filename(filename),
            created_at,
        ),
    )
    return get_attachment(attachment_id)


def get_attachment(attachment_id: str) -> Attachment:
    row = db.query_one(
        """
        SELECT id, file_path, mime_type, file_size, width, height, sha256, original_filename, linked_at, created_at
        FROM attachments
        WHERE id = ?
        """,
        (attachment_id,),
    )
    if row is None:
        raise ValueError(f"附件不存在：{attachment_id}")
    return _row_to_attachment(row)


def attachment_path(attachment_id: str) -> Path:
    attachment = get_attachment(attachment_id)
    path = db.WORKSPACE_DIR / attachment.file_path
    resolved_workspace = db.WORKSPACE_DIR.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_workspace):
        raise ValueError("附件路径非法")
    if not resolved_path.exists():
        raise ValueError(f"附件文件不存在：{attachment_id}")
    return resolved_path


def attach_to_post(post_id: str, attachment_ids: list[str] | None) -> None:
    ids = _normalize_attachment_ids(attachment_ids)
    if not ids:
        return
    if db.query_one("SELECT 1 FROM posts WHERE id = ?", (post_id,)) is None:
        raise ValueError(f"post 不存在：{post_id}")
    _link_many("post_attachments", "post_id", post_id, ids)


def attach_to_comment(comment_id: int, attachment_ids: list[str] | None) -> None:
    ids = _normalize_attachment_ids(attachment_ids)
    if not ids:
        return
    if db.query_one("SELECT 1 FROM comments WHERE id = ?", (comment_id,)) is None:
        raise ValueError(f"评论消息不存在：{comment_id}")
    _link_many("comment_attachments", "comment_id", int(comment_id), ids)


def attach_to_chat_message(message_id: int, attachment_ids: list[str] | None) -> None:
    ids = _normalize_attachment_ids(attachment_ids)
    if not ids:
        return
    if db.query_one("SELECT 1 FROM chat_messages WHERE id = ?", (message_id,)) is None:
        raise ValueError(f"私聊消息不存在：{message_id}")
    _link_many("chat_message_attachments", "message_id", int(message_id), ids)


def list_post_attachments(post_id: str) -> list[Attachment]:
    return _list_linked(
        """
        SELECT attachments.*
        FROM attachments
        JOIN post_attachments ON post_attachments.attachment_id = attachments.id
        WHERE post_attachments.post_id = ?
        ORDER BY post_attachments.sort_order, attachments.created_at, attachments.id
        """,
        (post_id,),
    )


def list_comment_attachments(comment_id: int) -> list[Attachment]:
    return _list_linked(
        """
        SELECT attachments.*
        FROM attachments
        JOIN comment_attachments ON comment_attachments.attachment_id = attachments.id
        WHERE comment_attachments.comment_id = ?
        ORDER BY comment_attachments.sort_order, attachments.created_at, attachments.id
        """,
        (int(comment_id),),
    )


def list_chat_message_attachments(message_id: int) -> list[Attachment]:
    return _list_linked(
        """
        SELECT attachments.*
        FROM attachments
        JOIN chat_message_attachments ON chat_message_attachments.attachment_id = attachments.id
        WHERE chat_message_attachments.message_id = ?
        ORDER BY chat_message_attachments.sort_order, attachments.created_at, attachments.id
        """,
        (int(message_id),),
    )


def has_attachments(attachment_ids: list[str] | None) -> bool:
    return bool(_normalize_attachment_ids(attachment_ids))


def validate_attachment_ids(attachment_ids: list[str] | None) -> list[str]:
    ids = _normalize_attachment_ids(attachment_ids)
    if not ids:
        return []
    existing = db.query_all(
        f"SELECT id FROM attachments WHERE id IN ({','.join('?' for _ in ids)})",
        tuple(ids),
    )
    existing_ids = {row["id"] for row in existing}
    missing = [attachment_id for attachment_id in ids if attachment_id not in existing_ids]
    if missing:
        raise ValueError(f"附件不存在：{', '.join(missing)}")
    return ids


def image_notice(count: int) -> str:
    if count <= 0:
        return ""
    unit = "张图片"
    return f"用户附带了 {count} {unit}，但当前没有可用的识图结果。不要描述、推断或声称看到了图片内容。"


def content_for_llm(content: str, attachment_count: int) -> str:
    body = content.strip()
    notice = image_notice(attachment_count)
    if body and notice:
        return f"{body}\n\n[{notice}]"
    return body or notice


def image_input_for_attachment(attachment: Attachment) -> ImageInput:
    path = attachment_path(attachment.id)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return ImageInput(
        attachment_id=attachment.id,
        mime_type=attachment.mime_type,
        file_path=path,
        width=attachment.width,
        height=attachment.height,
        file_size=attachment.file_size,
        data_url=f"data:{attachment.mime_type};base64,{encoded}",
    )


def image_inputs_for_attachments(attachments: list[Attachment]) -> list[ImageInput]:
    return [image_input_for_attachment(attachment) for attachment in attachments]


def cleanup_orphan_attachments(max_age_seconds: float = 24 * 3600) -> int:
    cutoff = db.now_ts() - max_age_seconds
    rows = db.query_all(
        """
        SELECT id, file_path
        FROM attachments
        WHERE linked_at IS NULL AND created_at < ?
        """,
        (cutoff,),
    )
    removed = 0
    for row in rows:
        path = db.WORKSPACE_DIR / row["file_path"]
        try:
            if path.exists():
                path.unlink()
        except OSError:
            continue
        db.execute("DELETE FROM attachments WHERE id = ?", (row["id"],))
        removed += 1
    return removed


def _open_image(file_bytes: bytes):
    try:
        from PIL import Image, ImageOps
        from PIL.Image import DecompressionBombError
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image uploads") from exc

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(file_bytes)) as image:
            image.load()
            image_format = _normalize_image_format(image.format)
            if image_format is None:
                raise UnsupportedImageFormatError(image.format)
            normalized = ImageOps.exif_transpose(image)
            return normalized.copy(), image_format
    except DecompressionBombError as exc:
        raise ValueError("图片像素尺寸过大") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("图片文件无效") from exc


def _encode_stored_image(image, image_format: str) -> tuple[bytes, str, int, int]:
    if image_format == "JPEG":
        return _encode_jpeg_to_limit(image.convert("RGB"))
    if image_format == "PNG":
        if _has_transparency(image):
            return _encode_transparent_png_to_limit(image.convert("RGBA"))
        png_image = image.convert("RGB")
        encoded = _encode_png(png_image)
        if len(encoded) <= MAX_STORED_IMAGE_BYTES:
            width, height = png_image.size
            return encoded, "PNG", width, height
        return _encode_jpeg_to_limit(png_image)
    raise ValueError("仅支持 JPEG 或 PNG 图片")


def _encode_jpeg_to_limit(image) -> tuple[bytes, str, int, int]:
    for candidate in _compression_candidates(image):
        for quality in JPEG_QUALITIES:
            encoded = _encode_jpeg(candidate, quality)
            if len(encoded) <= MAX_STORED_IMAGE_BYTES:
                width, height = candidate.size
                return encoded, "JPEG", width, height
    raise ValueError(COMPRESSION_TOO_LARGE_MESSAGE)


def _encode_transparent_png_to_limit(image) -> tuple[bytes, str, int, int]:
    for candidate in _compression_candidates(image):
        encoded = _encode_png(candidate)
        if len(encoded) <= MAX_STORED_IMAGE_BYTES:
            width, height = candidate.size
            return encoded, "PNG", width, height
    raise ValueError(COMPRESSION_TOO_LARGE_MESSAGE)


def _compression_candidates(image):
    width, height = image.size
    scale = min(1.0, MAX_COMPRESSED_IMAGE_SIDE / max(width, height))
    while True:
        next_width = max(1, int(width * scale))
        next_height = max(1, int(height * scale))
        if (next_width, next_height) == image.size:
            yield image
        else:
            yield _resize_image(image, next_width, next_height)
        if max(next_width, next_height) <= MIN_COMPRESSED_IMAGE_SIDE:
            break
        scale *= 0.85


def _resize_image(image, width: int, height: int):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image uploads") from exc
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _encode_jpeg(image, quality: int) -> bytes:
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def _encode_png(image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _has_transparency(image) -> bool:
    if image.mode in {"RGBA", "LA"}:
        alpha = image.getchannel("A")
        return alpha.getextrema()[0] < 255
    if image.mode == "P":
        return "transparency" in image.info
    return False


def _normalize_image_format(image_format: str | None) -> str | None:
    if not image_format:
        return None
    return READ_FORMAT_ALIASES.get(image_format.upper())


def _new_attachment_id() -> str:
    return f"att_{int(time.time() * 1000)}_{secrets.token_urlsafe(8)}"


def _relative_image_path(attachment_id: str, ext: str, created_at: float) -> Path:
    stamp = time.localtime(created_at)
    return Path("attachments") / "images" / f"{stamp.tm_year:04d}" / f"{stamp.tm_mon:02d}" / f"{attachment_id}{ext}"


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def _normalize_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type or None


def _is_allowed_upload_content_type(content_type: str) -> bool:
    return content_type in ACCEPTED_UPLOAD_MIME_TYPES or content_type in GENERIC_UPLOAD_MIME_TYPES


def _clean_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    cleaned = Path(filename).name.strip()
    return cleaned[:255] or None


def _normalize_attachment_ids(attachment_ids: list[str] | None) -> list[str]:
    if not attachment_ids:
        return []
    ids: list[str] = []
    for value in attachment_ids:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("attachment_ids 包含无效附件")
        attachment_id = value.strip()
        if attachment_id in ids:
            continue
        ids.append(attachment_id)
    if len(ids) > MAX_ATTACHMENTS_PER_ENTITY:
        raise ValueError(f"一次最多只能附加 {MAX_ATTACHMENTS_PER_ENTITY} 张图片")
    return ids


def _link_many(table: str, entity_column: str, entity_id, attachment_ids: list[str]) -> None:
    now = db.now_ts()
    validate_attachment_ids(attachment_ids)

    with db.transaction() as conn:
        conn.executemany(
            f"""
            INSERT OR IGNORE INTO {table}({entity_column}, attachment_id, sort_order)
            VALUES (?, ?, ?)
            """,
            [(entity_id, attachment_id, index) for index, attachment_id in enumerate(attachment_ids)],
        )
        conn.executemany(
            "UPDATE attachments SET linked_at = COALESCE(linked_at, ?) WHERE id = ?",
            [(now, attachment_id) for attachment_id in attachment_ids],
        )


def _list_linked(sql: str, params: tuple) -> list[Attachment]:
    return [_row_to_attachment(row) for row in db.query_all(sql, params)]


def _row_to_attachment(row) -> Attachment:
    return Attachment(
        id=row["id"],
        file_path=row["file_path"],
        mime_type=row["mime_type"],
        file_size=int(row["file_size"]),
        width=int(row["width"]),
        height=int(row["height"]),
        sha256=row["sha256"],
        original_filename=row["original_filename"],
        linked_at=float(row["linked_at"]) if row["linked_at"] is not None else None,
        created_at=float(row["created_at"]),
        url=f"/attachments/{row['id']}",
    )
