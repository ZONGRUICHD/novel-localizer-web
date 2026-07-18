from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from shiori.models import ExportArtifact, TranslationJob

from .lease import LeasedJob

PROCESSING_STATES = {
    "validating",
    "parsing",
    "translating",
    "reviewing",
    "assembling",
    "validating_output",
}
TERMINAL_STATES = {"completed", "failed", "cancelled"}


class SQLAlchemyJobRepository:
    """Lease the API's canonical ``translation_jobs`` table.

    A conditional UPDATE is used after selecting a candidate. This remains
    safe with SQLite's single writer and also avoids double leasing if the
    database is migrated to another engine for development.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_attempts: int = 5,
    ) -> None:
        self.session_factory = session_factory
        self.max_attempts = max_attempts

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def lease_next(self, *, owner: str, lease_seconds: float = 90.0) -> LeasedJob | None:
        now = self._now()
        expires = now + timedelta(seconds=lease_seconds)
        with self.session_factory() as session, session.begin():
            candidate = session.scalar(
                select(TranslationJob)
                .where(
                    TranslationJob.attempts < self.max_attempts,
                    or_(
                        TranslationJob.state == "queued",
                        and_(
                            TranslationJob.state.in_(PROCESSING_STATES),
                            TranslationJob.lease_expires_at.is_not(None),
                            TranslationJob.lease_expires_at <= now,
                        ),
                    ),
                )
                .order_by(TranslationJob.created_at.asc(), TranslationJob.id.asc())
                .limit(1)
            )
            if candidate is None:
                return None
            previous_state = candidate.state
            leased_attempt = candidate.attempts + 1
            condition = TranslationJob.state == "queued"
            if previous_state != "queued":
                condition = and_(
                    TranslationJob.state == previous_state,
                    TranslationJob.lease_expires_at <= now,
                )
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(TranslationJob)
                    .where(TranslationJob.id == candidate.id, condition)
                    .values(
                        state="validating",
                        current_stage="validating",
                        lease_owner=owner,
                        lease_expires_at=expires,
                        attempts=TranslationJob.attempts + 1,
                        error_code=None,
                        error_detail=None,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            return LeasedJob(
                id=candidate.id,
                kind=candidate.kind,
                payload={"project_id": candidate.project_id},
                checkpoint=dict(candidate.checkpoint or {}),
                attempts=leased_attempt,
                lease_owner=owner,
                lease_expires_at=expires.timestamp(),
            )

    def renew(self, job_id: str, *, owner: str, lease_seconds: float = 90.0) -> bool:
        now = self._now()
        with self.session_factory() as session, session.begin():
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(TranslationJob)
                    .where(
                        TranslationJob.id == job_id,
                        TranslationJob.lease_owner == owner,
                        TranslationJob.state.in_(PROCESSING_STATES),
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                ),
            )
            return result.rowcount == 1

    def checkpoint(
        self,
        job_id: str,
        *,
        owner: str,
        checkpoint: dict[str, Any],
        lease_seconds: float = 90.0,
    ) -> bool:
        now = self._now()
        with self.session_factory() as session, session.begin():
            job = session.get(TranslationJob, job_id)
            if (
                job is None
                or job.lease_owner != owner
                or job.state not in PROCESSING_STATES
                or job.cancellation_requested
            ):
                return False
            job.checkpoint = dict(checkpoint)
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            return True

    def set_stage(
        self,
        job_id: str,
        *,
        owner: str,
        stage: str,
        progress: float,
        checkpoint: dict[str, Any] | None = None,
        lease_seconds: float = 90.0,
    ) -> bool:
        if stage not in PROCESSING_STATES:
            raise ValueError(f"invalid processing stage: {stage}")
        now = self._now()
        with self.session_factory() as session, session.begin():
            job = session.get(TranslationJob, job_id)
            if job is None or job.lease_owner != owner or job.state in TERMINAL_STATES:
                return False
            job.state = stage
            job.current_stage = stage
            job.progress = min(max(progress, 0.0), 0.999)
            if checkpoint is not None:
                job.checkpoint = dict(checkpoint)
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            return True

    def complete(self, job_id: str, *, owner: str, result: dict[str, Any]) -> bool:
        now = self._now()
        with self.session_factory() as session, session.begin():
            job = session.get(TranslationJob, job_id)
            if job is None or job.lease_owner != owner or job.state not in PROCESSING_STATES:
                return False
            checkpoint = dict(job.checkpoint or {})
            checkpoint["result"] = result
            job.checkpoint = checkpoint
            awaiting_review = bool(result.get("awaiting_review"))
            job.state = "awaiting_review" if awaiting_review else "completed"
            job.current_stage = job.state
            job.progress = 0.98 if awaiting_review else 1.0
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            return True

    def fail(
        self,
        job_id: str,
        *,
        owner: str,
        code: str,
        message: str,
        retryable: bool,
        retry_delay: float = 0.0,
    ) -> bool:
        del retry_delay  # Translation client already performs bounded exponential backoff.
        now = self._now()
        with self.session_factory() as session, session.begin():
            job = session.get(TranslationJob, job_id)
            if job is None or job.lease_owner != owner or job.state not in PROCESSING_STATES:
                return False
            retry = retryable and job.attempts < self.max_attempts
            job.state = "queued" if retry else "failed"
            job.current_stage = "queued" if retry else "failed"
            job.error_code = code
            job.error_detail = message[:1000]
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            if job.kind == "export":
                export_id = (job.checkpoint or {}).get("export_id")
                artifact = session.get(ExportArtifact, export_id) if export_id else None
                if artifact is not None:
                    artifact.status = "queued" if retry else "failed"
                    artifact.validation = {"error_code": code}
            return True

    def cancelled(self, job_id: str, *, owner: str) -> bool:
        now = self._now()
        with self.session_factory() as session, session.begin():
            job = session.get(TranslationJob, job_id)
            if job is None or job.lease_owner != owner:
                return False
            job.state = "cancelled"
            job.current_stage = "cancelled"
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            return True

    def cancellation_requested(self, job_id: str, *, owner: str) -> bool:
        with self.session_factory() as session:
            job = session.get(TranslationJob, job_id)
            return bool(
                job is None
                or job.lease_owner != owner
                or job.cancellation_requested
                or job.state in {"paused", "cancelled"}
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.session_factory() as session:
            job = session.get(TranslationJob, job_id)
            if job is None:
                return None
            return {
                "id": job.id,
                "kind": job.kind,
                "state": job.state,
                "current_stage": job.current_stage,
                "progress": job.progress,
                "checkpoint": dict(job.checkpoint or {}),
                "attempts": job.attempts,
                "error_code": job.error_code,
                "error_detail": job.error_detail,
            }
