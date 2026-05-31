"""In-process API worker runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core import logging_service
from core.app_services import job_service, public_post_pipeline
from core.llm.types import LLMClient


@dataclass(frozen=True)
class ApiRuntime:
    config: dict
    client: LLMClient
    model: str
    worker: "JobWorker"
    vectorstore_initialized: bool


class JobWorker:
    """SQLite job worker for the API process."""

    def __init__(self, client: LLMClient, model: str, *, poll_interval: float = 0.5, concurrency: int = 1) -> None:
        self.client = client
        self.model = model
        self.poll_interval = poll_interval
        self.concurrency = max(1, min(int(concurrency), 4))
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        if not self._tasks or all(task.done() for task in self._tasks):
            self._stop.clear()
            self._tasks = [asyncio.create_task(self._run()) for _ in range(self.concurrency)]

    async def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=timeout)
        except asyncio.TimeoutError:
            for task in self._tasks:
                task.cancel()
            job_service.reset_running_to_pending()

    async def _run(self) -> None:
        job_service.reset_running_to_pending()
        while not self._stop.is_set():
            job = job_service.claim_next_pending()
            if job is None:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await asyncio.to_thread(public_post_pipeline.execute_job, job, self.client, self.model)
            except Exception as exc:
                job_service.mark_failed_or_retry(int(job["id"]), str(exc))
                logging_service.log_event(
                    "api_job_failed",
                    level="WARNING",
                    job_id=job["id"],
                    job_type=job["type"],
                    error=str(exc),
                )
            else:
                job_service.mark_succeeded(int(job["id"]))
