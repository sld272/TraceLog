"""In-process API worker runtime."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from core import attachment_service, logging_service
from core.app_services import job_service, public_post_pipeline
from core.llm.types import LLMClient

ORPHAN_ATTACHMENT_MAX_AGE_SECONDS = 24 * 3600
ORPHAN_ATTACHMENT_CLEANUP_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class ApiRuntime:
    config: dict
    client: LLMClient | None
    model: str | None
    worker: "JobWorker | None"
    vectorstore_initialized: bool
    configured: bool


class JobWorker:
    """SQLite job worker for the API process."""

    def __init__(
        self,
        client: LLMClient,
        model: str,
        *,
        poll_interval: float = 0.5,
        concurrency: int = 1,
        orphan_attachment_max_age: float = ORPHAN_ATTACHMENT_MAX_AGE_SECONDS,
        orphan_attachment_cleanup_interval: float = ORPHAN_ATTACHMENT_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        self.client = client
        self.model = model
        self.poll_interval = poll_interval
        self.concurrency = max(1, min(int(concurrency), 4))
        self.orphan_attachment_max_age = max(0.0, float(orphan_attachment_max_age))
        self.orphan_attachment_cleanup_interval = max(1.0, float(orphan_attachment_cleanup_interval))
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._cleanup_task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._tasks or all(task.done() for task in self._tasks):
            job_service.reset_running_to_pending()
            self._stop.clear()
            self._tasks = [asyncio.create_task(self._run()) for _ in range(self.concurrency)]
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._run_orphan_attachment_cleanup())

    async def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        await self._stop_job_tasks(timeout)
        await self._stop_cleanup_task(timeout)

    async def cleanup_orphan_attachments_once(self) -> int:
        try:
            removed = await asyncio.to_thread(
                attachment_service.cleanup_orphan_attachments,
                max_age_seconds=self.orphan_attachment_max_age,
            )
        except Exception as exc:
            logging_service.log_event(
                "orphan_attachments_cleanup_failed",
                level="WARNING",
                max_age_seconds=self.orphan_attachment_max_age,
                error=str(exc),
            )
            return 0
        if removed:
            logging_service.log_event(
                "orphan_attachments_cleaned",
                removed_count=removed,
                max_age_seconds=self.orphan_attachment_max_age,
            )
        return removed

    async def _run(self) -> None:
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
            # Emit pipeline_done if this was the last job for its post
            await asyncio.to_thread(public_post_pipeline.maybe_emit_pipeline_done_for_job, job)

    async def _run_orphan_attachment_cleanup(self) -> None:
        while not self._stop.is_set():
            await self.cleanup_orphan_attachments_once()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.orphan_attachment_cleanup_interval)

    async def _stop_job_tasks(self, timeout: float) -> None:
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=timeout)
        except asyncio.TimeoutError:
            for task in self._tasks:
                task.cancel()
            job_service.reset_running_to_pending()
        finally:
            self._tasks = []

    async def _stop_cleanup_task(self, timeout: float) -> None:
        if self._cleanup_task is None:
            return
        try:
            await asyncio.wait_for(self._cleanup_task, timeout=timeout)
        except asyncio.TimeoutError:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        finally:
            self._cleanup_task = None
