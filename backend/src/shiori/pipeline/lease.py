from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LeasedJob:
    id: str
    kind: str
    payload: dict[str, Any]
    checkpoint: dict[str, Any]
    attempts: int
    lease_owner: str
    lease_expires_at: float


class SQLiteLeaseRepository:
    """Durable single-worker queue using short, renewable SQLite leases."""

    def __init__(
        self,
        database: sqlite3.Connection | str | Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), timeout=30.0, check_same_thread=False)
        )
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self._lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipeline_jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                lease_owner TEXT,
                lease_expires_at REAL,
                not_before REAL NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_pipeline_jobs_ready
                ON pipeline_jobs(state, not_before, lease_expires_at, created_at);
            """
        )
        self.connection.commit()

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        job_id: str | None = None,
        max_attempts: int = 5,
    ) -> str:
        identifier = job_id or str(uuid.uuid4())
        now = self.clock()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO pipeline_jobs
                    (id, kind, state, payload_json, max_attempts, created_at, updated_at)
                VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    identifier,
                    kind,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    max_attempts,
                    now,
                    now,
                ),
            )
        return identifier

    def lease_next(self, *, owner: str, lease_seconds: float = 60.0) -> LeasedJob | None:
        now = self.clock()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    """
                    SELECT * FROM pipeline_jobs
                    WHERE attempts < max_attempts
                      AND not_before <= ?
                      AND (
                        state = 'queued'
                        OR (state = 'running' AND lease_expires_at <= ?)
                      )
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (now, now),
                ).fetchone()
                if row is None:
                    self.connection.commit()
                    return None
                expires = now + lease_seconds
                updated = self.connection.execute(
                    """
                    UPDATE pipeline_jobs
                    SET state='running', lease_owner=?, lease_expires_at=?,
                        attempts=attempts+1, updated_at=?
                    WHERE id=?
                      AND (state='queued' OR lease_expires_at <= ?)
                    """,
                    (owner, expires, now, row["id"], now),
                )
                if updated.rowcount != 1:
                    self.connection.rollback()
                    return None
                self.connection.commit()
                return LeasedJob(
                    id=str(row["id"]),
                    kind=str(row["kind"]),
                    payload=json.loads(row["payload_json"]),
                    checkpoint=json.loads(row["checkpoint_json"]),
                    attempts=int(row["attempts"]) + 1,
                    lease_owner=owner,
                    lease_expires_at=expires,
                )
            except Exception:
                self.connection.rollback()
                raise

    def renew(self, job_id: str, *, owner: str, lease_seconds: float = 60.0) -> bool:
        now = self.clock()
        with self.connection:
            result = self.connection.execute(
                """
                UPDATE pipeline_jobs
                SET lease_expires_at=?, updated_at=?
                WHERE id=? AND state='running' AND lease_owner=? AND lease_expires_at>?
                """,
                (now + lease_seconds, now, job_id, owner, now),
            )
        return result.rowcount == 1

    def checkpoint(
        self,
        job_id: str,
        *,
        owner: str,
        checkpoint: dict[str, Any],
        lease_seconds: float = 60.0,
    ) -> bool:
        now = self.clock()
        with self.connection:
            result = self.connection.execute(
                """
                UPDATE pipeline_jobs
                SET checkpoint_json=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND state='running' AND lease_owner=? AND lease_expires_at>?
                """,
                (
                    json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")),
                    now + lease_seconds,
                    now,
                    job_id,
                    owner,
                    now,
                ),
            )
        return result.rowcount == 1

    def complete(self, job_id: str, *, owner: str, result: dict[str, Any]) -> bool:
        now = self.clock()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE pipeline_jobs
                SET state='completed', result_json=?, lease_owner=NULL,
                    lease_expires_at=NULL, updated_at=?
                WHERE id=? AND state='running' AND lease_owner=?
                """,
                (
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    now,
                    job_id,
                    owner,
                ),
            )
        return updated.rowcount == 1

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
        now = self.clock()
        row = self.connection.execute(
            "SELECT attempts, max_attempts FROM pipeline_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return False
        state = (
            "queued" if retryable and int(row["attempts"]) < int(row["max_attempts"]) else "failed"
        )
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE pipeline_jobs
                SET state=?, error_code=?, error_message=?, not_before=?,
                    lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                WHERE id=? AND state='running' AND lease_owner=?
                """,
                (state, code, message[:1000], now + retry_delay, now, job_id, owner),
            )
        return updated.rowcount == 1

    def pause(self, job_id: str) -> bool:
        with self.connection:
            result = self.connection.execute(
                """UPDATE pipeline_jobs SET state='paused', lease_owner=NULL,
                lease_expires_at=NULL, updated_at=? WHERE id=? AND state IN ('queued','running')""",
                (self.clock(), job_id),
            )
        return result.rowcount == 1

    def resume(self, job_id: str) -> bool:
        with self.connection:
            result = self.connection.execute(
                "UPDATE pipeline_jobs SET state='queued', not_before=0, updated_at=? WHERE id=? AND state='paused'",
                (self.clock(), job_id),
            )
        return result.rowcount == 1

    def cancel(self, job_id: str) -> bool:
        with self.connection:
            result = self.connection.execute(
                """UPDATE pipeline_jobs SET state='cancelled', lease_owner=NULL,
                lease_expires_at=NULL, updated_at=? WHERE id=? AND state NOT IN ('completed','cancelled')""",
                (self.clock(), job_id),
            )
        return result.rowcount == 1

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM pipeline_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("payload_json", "checkpoint_json", "result_json"):
            value = result.pop(key)
            result[key.removesuffix("_json")] = json.loads(value) if value else None
        return result
