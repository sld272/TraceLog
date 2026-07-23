from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.web.server import create_production_app


class ProductionServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dist_dir = Path(self.tmp.name)
        (self.dist_dir / "assets").mkdir()
        (self.dist_dir / "index.html").write_text("<main>TraceLog</main>", encoding="utf-8")
        (self.dist_dir / "assets" / "app-abc123.js").write_text(
            "console.log('TraceLog')",
            encoding="utf-8",
        )
        self.lifespan_events: list[str] = []

        @asynccontextmanager
        async def lifespan(_: FastAPI):
            self.lifespan_events.append("startup")
            try:
                yield
            finally:
                self.lifespan_events.append("shutdown")

        self.api_app = FastAPI(lifespan=lifespan)

        @self.api_app.get("/health")
        async def health():
            return {"ok": True}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self) -> TestClient:
        create_app_patch = patch("core.web.server.create_app", return_value=self.api_app)
        create_app_patch.start()
        self.addCleanup(create_app_patch.stop)
        return TestClient(create_production_app(self.dist_dir))

    def test_api_is_reachable_under_api_prefix(self) -> None:
        with self._client() as client:
            response = client.get("/api/health")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True}, response.json())

    def test_root_and_unknown_deep_link_return_index(self) -> None:
        with self._client() as client:
            root_response = client.get("/")
            deep_link_response = client.get("/posts/saved")

        self.assertEqual(200, root_response.status_code)
        self.assertEqual("<main>TraceLog</main>", root_response.text)
        self.assertEqual("no-cache", root_response.headers["cache-control"])
        self.assertEqual(200, deep_link_response.status_code)
        self.assertEqual("<main>TraceLog</main>", deep_link_response.text)
        self.assertEqual("no-cache", deep_link_response.headers["cache-control"])

    def test_asset_has_immutable_cache_header(self) -> None:
        with self._client() as client:
            response = client.get("/assets/app-abc123.js")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "public, max-age=31536000, immutable",
            response.headers["cache-control"],
        )

    def test_missing_static_asset_stays_not_found(self) -> None:
        with self._client() as client:
            missing_file_response = client.get("/assets/missing.js")
            missing_extensionless_response = client.get("/assets/missing")

        self.assertEqual(404, missing_file_response.status_code)
        self.assertNotIn("TraceLog", missing_file_response.text)
        self.assertEqual(404, missing_extensionless_response.status_code)
        self.assertNotIn("TraceLog", missing_extensionless_response.text)

    def test_api_lifespan_is_started_and_stopped_by_root_app(self) -> None:
        with self._client():
            self.assertEqual(["startup"], self.lifespan_events)

        self.assertEqual(["startup", "shutdown"], self.lifespan_events)


if __name__ == "__main__":
    unittest.main()
