"""FastAPI application factory for TraceLog."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import deps
from api.routes import attachments, chat, comments, feedback, goals, jobs, memory, posts, schedule, settings, souls, suggestions
from core.version import APP_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    await deps.init_runtime()
    try:
        yield
    finally:
        await deps.shutdown_runtime()


def create_app() -> FastAPI:
    app = FastAPI(title="TraceLog API", version=APP_VERSION, lifespan=lifespan)
    app.include_router(posts.router)
    app.include_router(jobs.router)
    app.include_router(souls.router)
    app.include_router(goals.router)
    app.include_router(suggestions.router)
    app.include_router(chat.router)
    app.include_router(comments.router)
    app.include_router(feedback.router)
    app.include_router(attachments.router)
    app.include_router(settings.router)
    app.include_router(memory.router)
    app.include_router(schedule.router)
    return app


app = create_app()
