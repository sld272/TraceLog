"""In-process API worker runtime."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass

from core import attachment_service, logging_service
from core.app_services import job_service, public_post_pipeline
from core.llm.types import LLMClient

ORPHAN_ATTACHMENT_MAX_AGE_SECONDS = 24 * 3600
ORPHAN_ATTACHMENT_CLEANUP_INTERVAL_SECONDS = 3600
_ACTIVE_JOB_OWNERS: dict[int, int] = {}
_ACTIVE_JOB_IDS_LOCK = threading.Lock()


def _active_job_ids_snapshot() -> set[int]:
    with _ACTIVE_JOB_IDS_LOCK:
        return set(_ACTIVE_JOB_OWNERS)


def _claim_active_job(job_id: int) -> None:
    with _ACTIVE_JOB_IDS_LOCK:
        _ACTIVE_JOB_OWNERS[job_id] = _ACTIVE_JOB_OWNERS.get(job_id, 0) + 1


def _release_active_job(job_id: int) -> None:
    with _ACTIVE_JOB_IDS_LOCK:
        remaining_owners = _ACTIVE_JOB_OWNERS[job_id] - 1
        if remaining_owners:
            _ACTIVE_JOB_OWNERS[job_id] = remaining_owners
        else:
            del _ACTIVE_JOB_OWNERS[job_id]


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
            job_service.reset_orphaned_running_to_pending(_active_job_ids_snapshot())
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
            await asyncio.to_thread(self._execute_claimed_job, job)

    def _execute_claimed_job(self, job: dict) -> None:
        """Run one claimed job through its terminal status and pipeline event.

        The owner is registered here rather than before ``to_thread`` so that
        registering and clearing it always happen together. Splitting them
        leaves a window where a cancelled task registers an owner nothing will
        ever clear, and that owner is precisely what stops a later worker from
        recovering the row — the job would stay ``running`` forever.
        """
        job_id = int(job["id"])
        _claim_active_job(job_id)
        try:
            try:
                public_post_pipeline.execute_job(job, self.client, self.model)
            except Exception as exc:
                if job["type"] == job_service.TYPE_RUN_MEMORY_RECONCILE:
                    job_service.mark_memory_reconcile_failed_or_retry(job_id, str(exc))
                else:
                    job_service.mark_failed_or_retry(job_id, str(exc))
                logging_service.log_event(
                    "api_job_failed",
                    level="WARNING",
                    job_id=job_id,
                    job_type=job["type"],
                    error=str(exc),
                )
            else:
                job_service.mark_succeeded(job_id)
            public_post_pipeline.maybe_emit_pipeline_done_for_job(job)
        finally:
            _release_active_job(job_id)

    async def _run_orphan_attachment_cleanup(self) -> None:
        while not self._stop.is_set():
            await self.cleanup_orphan_attachments_once()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.orphan_attachment_cleanup_interval)

    async def _stop_job_tasks(self, timeout: float) -> None:
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        if pending:
            # A settings save can time out while an embedding or LLM job is still
            # running inside asyncio.to_thread; cancelling this Task would not stop
            # that synchronous executor, so keep its owner and running row intact.
            for task in done:
                with contextlib.suppress(asyncio.CancelledError):
                    task.exception()
            self._tasks = list(pending)
            return
        try:
            await asyncio.gather(*done)
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
