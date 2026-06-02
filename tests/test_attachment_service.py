from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

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

    def test_upload_rejects_non_image(self) -> None:
        with self.assertRaises(ValueError):
            attachment_service.upload_image(b"not an image", content_type="image/png", filename="x.png")

    def test_attach_to_post_lists_attachments(self) -> None:
        attachment = attachment_service.upload_image(_image_bytes("PNG"), content_type="image/png")
        post_id = record_service.save_post("带图帖子", index_immediately=False)

        attachment_service.attach_to_post(post_id, [attachment.id])

        linked = attachment_service.list_post_attachments(post_id)
        self.assertEqual([attachment.id], [item.id for item in linked])
        self.assertIsNotNone(attachment_service.get_attachment(attachment.id).linked_at)


def _image_bytes(image_format: str) -> bytes:
    image = Image.new("RGB", (12, 8), color=(120, 80, 40))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()
