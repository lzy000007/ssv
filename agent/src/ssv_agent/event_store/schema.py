"""SQLite 表结构、迁移与兼容查询模型。"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    ingress_id TEXT,
    source TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    frame_id INTEGER NOT NULL,
    stream_generation INTEGER,
    source_pts INTEGER,
    event_type TEXT,
    severity TEXT,
    rule_id TEXT,
    rule_version TEXT,
    rule_facts_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    verdict TEXT,
    confidence REAL,
    result_path TEXT,
    created_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    class_name TEXT NOT NULL,
    class_id INTEGER NOT NULL,
    confidence REAL NOT NULL,
    bbox_json TEXT NOT NULL,
    track_id INTEGER NOT NULL DEFAULT -1,
    track_state TEXT,
    occluded INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, class_name, track_id, confidence, bbox_json)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    evidence_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('frame', 'clip')),
    path TEXT NOT NULL,
    mime_type TEXT,
    available INTEGER NOT NULL DEFAULT 0,
    size INTEGER,
    mtime REAL,
    sha256 TEXT,
    source_pts_start INTEGER,
    source_pts_end INTEGER,
    stream_generation INTEGER,
    UNIQUE (event_id, kind, path)
);

CREATE TABLE IF NOT EXISTS review_results (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    verdict TEXT,
    confidence REAL,
    evidence_status TEXT,
    explanation TEXT,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    claims_json TEXT NOT NULL DEFAULT '[]',
    policy_id TEXT,
    model_id TEXT,
    parsed_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    revision INTEGER NOT NULL,
    policy_id TEXT,
    model_id TEXT,
    verdict TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_status TEXT NOT NULL,
    explanation TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    claims_json TEXT NOT NULL,
    result_path TEXT,
    created_ms INTEGER NOT NULL,
    UNIQUE (event_id, revision)
);

CREATE TABLE IF NOT EXISTS durable_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('review', 'index', 'evidence_extract')),
    entity_id TEXT NOT NULL REFERENCES events(event_id),
    entity_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'processing', 'completed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_ms INTEGER,
    available_at_ms INTEGER NOT NULL,
    last_error TEXT,
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    UNIQUE (kind, entity_id, entity_revision)
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_detections_event ON detections(event_id);
CREATE INDEX IF NOT EXISTS idx_evidence_event ON evidence(event_id);
"""


_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "events": {
        "ingress_id": "TEXT",
        "stream_generation": "INTEGER",
        "source_pts": "INTEGER",
        "rule_id": "TEXT",
        "rule_version": "TEXT",
        "rule_facts_json": "TEXT NOT NULL DEFAULT '{}'",
        "revision": "INTEGER NOT NULL DEFAULT 0",
    },
    "evidence": {
        "evidence_id": "TEXT",
        "mime_type": "TEXT",
        "sha256": "TEXT",
        "source_pts_start": "INTEGER",
        "source_pts_end": "INTEGER",
        "stream_generation": "INTEGER",
    },
    "review_results": {
        "evidence_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "claims_json": "TEXT NOT NULL DEFAULT '[]'",
        "policy_id": "TEXT",
        "model_id": "TEXT",
    },
    "reviews": {
        "policy_id": "TEXT",
        "model_id": "TEXT",
    },
}


def migrate_schema(connection: sqlite3.Connection) -> None:
    """以可重复方式将历史数据库升级到当前账本结构。"""
    connection.executescript(SCHEMA_SQL)
    durable_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'durable_jobs'"
    ).fetchone()[0]
    if "evidence_extract" not in durable_sql:
        # SQLite cannot alter a CHECK constraint. Rebuild the table in one transaction,
        # explicitly copying every durable column so leases and retry history survive.
        connection.execute("BEGIN")
        try:
            connection.execute(
                """
                CREATE TABLE durable_jobs_new (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('review', 'index', 'evidence_extract')),
                    entity_id TEXT NOT NULL REFERENCES events(event_id),
                    entity_revision INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'processing', 'completed', 'dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_ms INTEGER,
                    available_at_ms INTEGER NOT NULL,
                    last_error TEXT,
                    created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    UNIQUE (kind, entity_id, entity_revision)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO durable_jobs_new (
                    job_id, kind, entity_id, entity_revision, state, attempts,
                    lease_owner, lease_expires_ms, available_at_ms, last_error,
                    created_ms, updated_ms
                )
                SELECT job_id, kind, entity_id, entity_revision, state, attempts,
                    lease_owner, lease_expires_ms, available_at_ms, last_error,
                    created_ms, updated_ms
                FROM durable_jobs
                """
            )
            connection.execute("DROP TABLE durable_jobs")
            connection.execute("ALTER TABLE durable_jobs_new RENAME TO durable_jobs")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_durable_jobs_claim
                    ON durable_jobs(kind, state, available_at_ms, lease_expires_ms, job_id)
                """
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    for table, columns in _COLUMN_MIGRATIONS.items():
        known = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in known:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    connection.execute(
        "UPDATE evidence SET evidence_id = 'legacy-' || id WHERE evidence_id IS NULL"
    )
    connection.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_ingress_id ON events(ingress_id)
            WHERE ingress_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_event_evidence_id
            ON evidence(event_id, evidence_id) WHERE evidence_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_reviews_event ON reviews(event_id, revision DESC);
        CREATE INDEX IF NOT EXISTS idx_durable_jobs_claim
            ON durable_jobs(kind, state, available_at_ms, lease_expires_ms, job_id);
        """
    )


class EventQuery(BaseModel):
    """事件列表筛选条件。"""

    source: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    event_type: str | None = None
    status: str | None = None
    verdict: str | None = None
    class_name: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
