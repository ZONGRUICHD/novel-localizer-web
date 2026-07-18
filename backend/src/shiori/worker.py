from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from shiori.config import get_settings
from shiori.db import build_engine, build_session_factory
from shiori.documents.errors import RATE_LIMITED, DocumentError
from shiori.pipeline.core_jobs import SQLAlchemyJobRepository
from shiori.pipeline.execution import CorePipelineRuntime, JobCancellationRequested
from shiori.pipeline.lease import LeasedJob

logger = logging.getLogger("shiori.worker")
CheckpointWriter = Callable[[dict[str, Any]], None]
JobHandler = Callable[[LeasedJob, CheckpointWriter], dict[str, Any]]


class JobRepository(Protocol):
    def lease_next(self, *, owner: str, lease_seconds: float = 60.0) -> LeasedJob | None: ...

    def checkpoint(
        self,
        job_id: str,
        *,
        owner: str,
        checkpoint: dict[str, Any],
        lease_seconds: float = 60.0,
    ) -> bool: ...

    def complete(self, job_id: str, *, owner: str, result: dict[str, Any]) -> bool: ...

    def fail(
        self,
        job_id: str,
        *,
        owner: str,
        code: str,
        message: str,
        retryable: bool,
        retry_delay: float = 0.0,
    ) -> bool: ...

    def cancelled(self, job_id: str, *, owner: str) -> bool: ...


@dataclass(slots=True)
class RetryableJobError(Exception):
    code: str
    message: str
    retry_delay: float = 5.0


class ShioriWorker:
    def __init__(
        self,
        repository: JobRepository,
        handlers: Mapping[str, JobHandler],
        *,
        owner: str | None = None,
        lease_seconds: float = 90.0,
        poll_seconds: float = 1.0,
    ) -> None:
        self.repository = repository
        self.handlers = dict(handlers)
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = min(max(poll_seconds, 0.05), 10.0)
        self._stopped = threading.Event()

    def stop(self) -> None:
        self._stopped.set()

    def run_once(self) -> bool:
        job = self.repository.lease_next(owner=self.owner, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        handler = self.handlers.get(job.kind)
        if handler is None:
            self.repository.fail(
                job.id,
                owner=self.owner,
                code="UNKNOWN_JOB_KIND",
                message=f"No worker handler is registered for {job.kind!r}.",
                retryable=False,
            )
            return True

        def write_checkpoint(value: dict[str, Any]) -> None:
            if not self.repository.checkpoint(
                job.id,
                owner=self.owner,
                checkpoint=value,
                lease_seconds=self.lease_seconds,
            ):
                raise RuntimeError("The job lease was lost while writing a checkpoint.")

        try:
            result = handler(job, write_checkpoint)
            if not self.repository.complete(job.id, owner=self.owner, result=result):
                logger.warning("job lease lost before completion", extra={"job_id": job.id})
        except RetryableJobError as exc:
            self.repository.fail(
                job.id,
                owner=self.owner,
                code=exc.code,
                message=exc.message,
                retryable=True,
                retry_delay=exc.retry_delay,
            )
        except JobCancellationRequested:
            self.repository.cancelled(job.id, owner=self.owner)
        except DocumentError as exc:
            self.repository.fail(
                job.id,
                owner=self.owner,
                code=exc.code,
                message=exc.message,
                retryable=exc.code == RATE_LIMITED,
                retry_delay=10.0,
            )
        except Exception as exc:  # pragma: no cover - final process boundary
            logger.exception("job failed", extra={"job_id": job.id, "kind": job.kind})
            self.repository.fail(
                job.id,
                owner=self.owner,
                code="WORKER_ERROR",
                message=type(exc).__name__,
                retryable=False,
            )
        return True

    def run_forever(self) -> None:
        while not self._stopped.is_set():
            if not self.run_once():
                self._stopped.wait(self.poll_seconds)


def run() -> None:
    """Systemd entry point backed by the canonical API database."""

    logging.basicConfig(level=os.environ.get("SHIORI_LOG_LEVEL", "INFO"))
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    repository = SQLAlchemyJobRepository(session_factory)
    runtime = CorePipelineRuntime(
        settings=settings,
        session_factory=session_factory,
        jobs=repository,
    )
    worker = ShioriWorker(repository, runtime.handlers)
    try:
        while not worker._stopped.is_set():
            handled_maintenance = runtime.process_one_library_profile()
            handled_job = worker.run_once()
            if not handled_maintenance and not handled_job:
                worker._stopped.wait(worker.poll_seconds)
    except KeyboardInterrupt:
        worker.stop()
    finally:
        engine.dispose()


if __name__ == "__main__":
    run()
