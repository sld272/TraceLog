"""Production ASGI application for the TraceLog API and built frontend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from api.app import create_app


class SPAStaticFiles(StaticFiles):
    """Serve Vite assets and fall back to index.html for SPA-style paths."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            request_path = scope.get("path", "")
            is_asset_request = request_path == "/assets" or request_path.startswith(
                "/assets/"
            )
            if (
                exc.status_code != 404
                or scope["method"] != "GET"
                or is_asset_request
                or Path(path).suffix
            ):
                raise
            return await super().get_response("index.html", scope)


def create_production_app(dist_dir: Path) -> FastAPI:
    """Combine the API and built frontend into one production application."""

    api_app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with api_app.router.lifespan_context(api_app):
            yield

    root = FastAPI(lifespan=lifespan)

    @root.middleware("http")
    async def add_static_cache_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        path = request.url.path
        if response.status_code == 200 and path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif (
            response.status_code == 200
            and not path.startswith("/api/")
            and response.headers.get("content-type", "").startswith("text/html")
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response

    root.mount("/api", api_app)
    root.mount("/", SPAStaticFiles(directory=dist_dir, html=True))
    return root
