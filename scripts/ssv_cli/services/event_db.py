"""SQLite EventLedger runtime-state administration."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_RUNTIME_TABLES = (
    "events",
    "detections",
    "evidence",
    "reviews",
    "review_results",
    "durable_jobs",
)
_REQUIRED_COLUMNS = {
    "events": {
        "event_id",
        "source",
        "timestamp_ms",
        "frame_id",
        "status",
        "created_ms",
    },
    "detections": {
        "id",
        "event_id",
        "class_name",
        "class_id",
        "confidence",
        "bbox_json",
        "track_id",
    },
    "evidence": {"id", "event_id", "kind", "path"},
    "reviews": {
        "review_id",
        "event_id",
        "revision",
        "verdict",
        "confidence",
        "evidence_status",
        "explanation",
        "evidence_ids_json",
        "claims_json",
        "created_ms",
    },
    "review_results": {
        "event_id",
        "verdict",
        "confidence",
        "evidence_status",
        "explanation",
        "parsed_at_ms",
    },
    "durable_jobs": {
        "job_id",
        "kind",
        "entity_id",
        "entity_revision",
        "state",
        "attempts",
        "available_at_ms",
        "created_ms",
        "updated_ms",
    },
}
_DELETE_ORDER = (
    "review_results",
    "reviews",
    "detections",
    "evidence",
    "durable_jobs",
    "events",
)


class EventDbError(RuntimeError):
    """The configured EventLedger database cannot be inspected or cleared."""


@dataclass(frozen=True)
class DurableJobStatus:
    """按任务类型和状态聚合的只读诊断信息。"""

    kind: str
    state: str
    count: int
    max_attempts: int
    oldest_updated_ms: int | None
    expired_leases: int = 0


@dataclass(frozen=True)
class DeadLetterStatus:
    """不含事件标识和自由文本错误的死信摘要。"""

    job_id: int
    kind: str
    entity_revision: int
    attempts: int
    updated_ms: int
    error_code: str


@dataclass(frozen=True)
class EventDbStatus:
    path: Path
    exists: bool
    events: int = 0
    detections: int = 0
    evidence: int = 0
    reviews: int = 0
    review_results: int = 0
    durable_jobs: int = 0
    durable_job_states: tuple[DurableJobStatus, ...] = ()
    dead_letters: tuple[DeadLetterStatus, ...] = ()

    @property
    def total_rows(self) -> int:
        return sum(
            (
                self.events,
                self.detections,
                self.evidence,
                self.reviews,
                self.review_results,
                self.durable_jobs,
            )
        )


@dataclass(frozen=True)
class EventDbCleanupResult:
    status_before: EventDbStatus
    deleted_rows: int
    dry_run: bool


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _validate_schema(connection: sqlite3.Connection) -> set[str]:
    table_names = _table_names(connection)
    missing = sorted(set(_RUNTIME_TABLES) - table_names)
    if missing:
        missing_tables = ", ".join(missing)
        raise EventDbError(f"不是受支持的 SSV EventLedger 数据库，缺少表: {missing_tables}")
    invalid = {
        table: sorted(required - _column_names(connection, table))
        for table, required in _REQUIRED_COLUMNS.items()
    }
    invalid = {table: columns for table, columns in invalid.items() if columns}
    if invalid:
        details = "; ".join(
            f"{table} 缺少列 {', '.join(columns)}" for table, columns in invalid.items()
        )
        raise EventDbError(f"不是受支持的 SSV EventLedger 数据库: {details}")
    return table_names


def _durable_job_statuses(connection: sqlite3.Connection) -> tuple[DurableJobStatus, ...]:
    durable_job_columns = _column_names(connection, "durable_jobs")
    has_lease_column = "lease_expires_ms" in durable_job_columns
    expired_leases_sql = (
        "SUM(CASE WHEN state = 'processing' AND lease_expires_ms IS NOT NULL "
        "AND lease_expires_ms <= ? THEN 1 ELSE 0 END)"
        if has_lease_column
        else "0"
    )
    expired_leases_params = (time.time_ns() // 1_000_000,) if has_lease_column else ()
    rows = connection.execute(
        f"""SELECT kind, state, COUNT(*) AS count, MAX(attempts) AS max_attempts,
                   MIN(updated_ms) AS oldest_updated_ms,
                   {expired_leases_sql} AS expired_leases
            FROM durable_jobs
            GROUP BY kind, state
            ORDER BY kind, state""",
        expired_leases_params,
    ).fetchall()
    return tuple(
        DurableJobStatus(
            kind=str(row[0]),
            state=str(row[1]),
            count=int(row[2]),
            max_attempts=int(row[3] or 0),
            oldest_updated_ms=(
                int(row[4])
                if row[4] is not None
                else None
            ),
            expired_leases=int(row[5] or 0),
        )
        for row in rows
    )


_DEAD_LETTER_JOB_KINDS = frozenset({"evidence_extract", "index", "review"})
_DEAD_LETTER_ERROR_CODES = frozenset(
    {
        "evidence_extraction_failed",
        "index_failed",
        "lease_heartbeat_failed",
        "max_retries_exceeded",
        "review_failed",
    }
)


def _dead_letter_statuses(
    connection: sqlite3.Connection,
) -> tuple[DeadLetterStatus, ...]:
    columns = _column_names(connection, "durable_jobs")
    error_expression = "last_error" if "last_error" in columns else "NULL"
    rows = connection.execute(
        f"""SELECT job_id, kind, entity_revision, attempts, updated_ms,
                   {error_expression} AS last_error
            FROM durable_jobs
            WHERE state = 'dead'
            ORDER BY updated_ms DESC, job_id DESC
            LIMIT 5"""
    ).fetchall()
    results: list[DeadLetterStatus] = []
    for row in rows:
        error_code = str(row[5]) if row[5] is not None else ""
        if error_code not in _DEAD_LETTER_ERROR_CODES:
            error_code = "legacy_unclassified"
        results.append(
            DeadLetterStatus(
                job_id=int(row[0]),
                kind=(
                    str(row[1])
                    if str(row[1]) in _DEAD_LETTER_JOB_KINDS
                    else "unknown"
                ),
                entity_revision=int(row[2]),
                attempts=int(row[3]),
                updated_ms=int(row[4]),
                error_code=error_code,
            )
        )
    return tuple(results)


def _status_from_connection(
    path: Path,
    connection: sqlite3.Connection,
    *,
    validate: bool = True,
) -> EventDbStatus:
    if validate:
        _validate_schema(connection)
    counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _RUNTIME_TABLES
    }
    return EventDbStatus(
        path=path,
        exists=True,
        durable_job_states=_durable_job_statuses(connection),
        dead_letters=_dead_letter_statuses(connection),
        **counts,
    )


class EventDbAdmin:
    """Read and clear only the runtime tables owned by EventLedger."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def status(self) -> EventDbStatus:
        if not self.path.is_file():
            return EventDbStatus(path=self.path, exists=False)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(_readonly_uri(self.path), uri=True, timeout=5.0)
            return _status_from_connection(self.path, connection)
        except (EventDbError, OSError, sqlite3.Error) as exc:
            raise EventDbError(f"无法读取 SQLite EventLedger: {self.path}: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def reset(self, *, dry_run: bool = False) -> EventDbCleanupResult:
        before = self.status()
        if dry_run or not before.exists:
            return EventDbCleanupResult(
                status_before=before,
                deleted_rows=0,
                dry_run=dry_run,
            )

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.execute("BEGIN IMMEDIATE")
            table_names = _validate_schema(connection)
            for table in _DELETE_ORDER:
                connection.execute(f"DELETE FROM {table}")
            if "sqlite_sequence" in table_names:
                connection.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN (?, ?, ?, ?, ?, ?)",
                    _RUNTIME_TABLES,
                )
            connection.commit()
        except (EventDbError, OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.rollback()
            raise EventDbError(f"无法清理 SQLite EventLedger: {self.path}: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

        return EventDbCleanupResult(
            status_before=before,
            deleted_rows=before.total_rows,
            dry_run=False,
        )
