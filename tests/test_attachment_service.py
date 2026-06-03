from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from core import attachment_service, db, record_service, soul_memory_service, soul_service


class AttachmentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_service_memories_dir = soul_service.SOUL_MEMORIES_DIR
        self.old_memory_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"

        db.init_db()
        soul_service.sync_souls()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        self.tmp.cleanup()

    def test_upload_image_stores_clean_metadata_and_file(self) -> None:
        attachment = attachment_service.upload_image(
            _image_bytes("JPEG"),
            content_type="image/jpeg",
            filename="../photo.jpg",
        )

        self.assertEqual("image/jpeg", attachment.mime_type)
        self.assertEqual("photo.jpg", attachment.original_filename)
        self.assertEqual(12, attachment.width)
        self.assertEqual(8, attachment.height)
        self.assertTrue((self.workspace / attachment.file_path).exists())
        with Image.open(self.workspace / attachment.file_path) as image:
            self.assertEqual([], list(image.getexif().items()))

    def test_upload_accepts_uppercase_jpg_filename_and_mime_type(self) -> None:
        attachment = attachment_service.upload_image(
            _image_bytes("JPEG"),
            content_type="IMAGE/JPEG",
            filename="PHOTO.JPG",
        )

        self.assertEqual("image/jpeg", attachment.mime_type)
        self.assertEqual("PHOTO.JPG", attachment.original_filename)
        self.assertTrue(attachment.file_path.endswith(".jpg"))

    def test_upload_accepts_valid_image_with_generic_browser_mime_type(self) -> None:
        attachment = attachment_service.upload_image(
            _image_bytes("JPEG"),
            content_type="application/octet-stream",
            filename="PHOTO.JPG",
        )

        self.assertEqual("image/jpeg", attachment.mime_type)
        self.assertTrue(attachment.file_path.endswith(".jpg"))

    def test_upload_accepts_camera_jpg_detected_as_mpo(self) -> None:
        real_open = Image.open

        def open_as_mpo(*args, **kwargs):
            image = real_open(*args, **kwargs)
            image.format = "MPO"
            return image

        with patch("PIL.Image.open", side_effect=open_as_mpo):
            attachment = attachment_service.upload_image(
                _image_bytes("JPEG"),
                content_type="image/jpeg",
                filename="DSC_9843.JPG",
            )

        self.assertEqual("image/jpeg", attachment.mime_type)
        self.assertEqual("DSC_9843.JPG", attachment.original_filename)
        self.assertTrue(attachment.file_path.endswith(".jpg"))

    def test_upload_rejects_non_image(self) -> None:
        with self.assertRaises(ValueError):
            attachment_service.upload_image(b"not an image", content_type="image/png", filename="x.png")

    def test_upload_rejects_specific_unsupported_image_mime_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "仅支持 JPEG 或 PNG 图片"):
            attachment_service.upload_image(_image_bytes("PNG"), content_type="image/gif", filename="x.gif")

    def test_upload_large_jpeg_compresses_to_stored_limit(self) -> None:
        attachment = attachment_service.upload_image(
            _noisy_image_bytes("JPEG", "RGB", (3000, 2200)),
            content_type="image/jpeg",
        )

        self.assertEqual("image/jpeg", attachment.mime_type)
        self.assertLessEqual(attachment.file_size, attachment_service.MAX_STORED_IMAGE_BYTES)
        self.assertLessEqual(max(attachment.width, attachment.height), attachment_service.MAX_COMPRESSED_IMAGE_SIDE)
        with Image.open(self.workspace / attachment.file_path) as image:
            self.assertEqual("JPEG", image.format)
            self.assertEqual((attachment.width, attachment.height), image.size)

    def test_upload_jpeg_above_old_pixel_limit_compresses(self) -> None:
        attachment = attachment_service.upload_image(
            _solid_image_bytes("JPEG", (7000, 4000)),
            content_type="image/jpeg",
        )

        self.assertEqual("image/jpeg", attachment.mime_type)
        self.assertLessEqual(attachment.file_size, attachment_service.MAX_STORED_IMAGE_BYTES)
        self.assertLessEqual(max(attachment.width, attachment.height), attachment_service.MAX_COMPRESSED_IMAGE_SIDE)

    def test_upload_large_rgb_png_can_store_as_jpeg(self) -> None:
        attachment = attachment_service.upload_image(
            _noisy_image_bytes("PNG", "RGB", (2200, 2200)),
            content_type="image/png",
        )

        self.assertEqual("image/jpeg", attachment.mime_type)
        self.assertTrue(attachment.file_path.endswith(".jpg"))
        self.assertLessEqual(attachment.file_size, attachment_service.MAX_STORED_IMAGE_BYTES)
        with Image.open(self.workspace / attachment.file_path) as image:
            self.assertEqual("JPEG", image.format)

    def test_upload_large_transparent_png_preserves_alpha(self) -> None:
        attachment = attachment_service.upload_image(
            _noisy_image_bytes("PNG", "RGBA", (1700, 1700)),
            content_type="image/png",
        )

        self.assertEqual("image/png", attachment.mime_type)
        self.assertLessEqual(attachment.file_size, attachment_service.MAX_STORED_IMAGE_BYTES)
        with Image.open(self.workspace / attachment.file_path) as image:
            self.assertEqual("PNG", image.format)
            self.assertIn(image.mode, {"RGBA", "LA"})
            self.assertLess(image.getchannel("A").getextrema()[0], 255)

    def test_upload_rejects_when_compressed_image_still_exceeds_limit(self) -> None:
        with patch.object(attachment_service, "MAX_STORED_IMAGE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "图片压缩后体积仍超过5MB！"):
                attachment_service.upload_image(_image_bytes("JPEG"), content_type="image/jpeg")

    def test_upload_rejects_when_original_file_exceeds_upload_limit(self) -> None:
        image_bytes = _image_bytes("PNG")

        with patch.object(attachment_service, "MAX_UPLOAD_IMAGE_BYTES", len(image_bytes) - 1):
            with self.assertRaisesRegex(ValueError, "图片不能超过 50MB"):
                attachment_service.upload_image(image_bytes, content_type="image/png")

    def test_attach_to_post_lists_attachments(self) -> None:
        attachment = attachment_service.upload_image(_image_bytes("PNG"), content_type="image/png")
        post_id = record_service.save_post("带图帖子", index_immediately=False)

        attachment_service.attach_to_post(post_id, [attachment.id])

        linked = attachment_service.list_post_attachments(post_id)
        self.assertEqual([attachment.id], [item.id for item in linked])
        self.assertIsNotNone(attachment_service.get_attachment(attachment.id).linked_at)

    def test_cleanup_orphan_attachments_removes_only_expired_unlinked_files(self) -> None:
        old_orphan = attachment_service.upload_image(_image_bytes("PNG"), content_type="image/png")
        fresh_orphan = attachment_service.upload_image(_image_bytes("PNG"), content_type="image/png")
        linked = attachment_service.upload_image(_image_bytes("PNG"), content_type="image/png")
        post_id = record_service.save_post("带图帖子", index_immediately=False)
        attachment_service.attach_to_post(post_id, [linked.id])

        cutoff_age = 24 * 3600
        old_created_at = db.now_ts() - cutoff_age - 1
        db.execute("UPDATE attachments SET created_at = ? WHERE id = ?", (old_created_at, old_orphan.id))
        db.execute("UPDATE attachments SET created_at = ? WHERE id = ?", (old_created_at, linked.id))

        removed = attachment_service.cleanup_orphan_attachments(max_age_seconds=cutoff_age)

        self.assertEqual(1, removed)
        self.assertFalse((self.workspace / old_orphan.file_path).exists())
        self.assertIsNone(db.query_one("SELECT 1 FROM attachments WHERE id = ?", (old_orphan.id,)))
        self.assertTrue((self.workspace / fresh_orphan.file_path).exists())
        self.assertIsNotNone(attachment_service.get_attachment(fresh_orphan.id))
        self.assertTrue((self.workspace / linked.file_path).exists())
        self.assertIsNotNone(attachment_service.get_attachment(linked.id))

    def test_cleanup_orphan_attachments_deletes_db_row_even_if_file_is_missing(self) -> None:
        orphan = attachment_service.upload_image(_image_bytes("PNG"), content_type="image/png")
        (self.workspace / orphan.file_path).unlink()
        db.execute("UPDATE attachments SET created_at = ? WHERE id = ?", (db.now_ts() - 25 * 3600, orphan.id))

        removed = attachment_service.cleanup_orphan_attachments(max_age_seconds=24 * 3600)

        self.assertEqual(1, removed)
        self.assertIsNone(db.query_one("SELECT 1 FROM attachments WHERE id = ?", (orphan.id,)))


def _image_bytes(image_format: str) -> bytes:
    image = Image.new("RGB", (12, 8), color=(120, 80, 40))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _solid_image_bytes(image_format: str, size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, color=(120, 80, 40))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _noisy_image_bytes(image_format: str, mode: str, size: tuple[int, int]) -> bytes:
    width, height = size
    channels = len(mode)
    image = Image.frombytes(mode, size, os.urandom(width * height * channels))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()
