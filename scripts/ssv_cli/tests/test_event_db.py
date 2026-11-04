from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.ssv_cli.services.event_db import EventDbAdmin, EventDbError

_TABLES = (
    "events",
    "detections",
    "evidence",
    "reviews",
    "review_results",
    "durable_jobs",
)


def _create_event_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                frame_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_ms INTEGER NOT NULL
            );
            CREATE TABLE detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                class_name TEXT NOT NULL,
                class_id INTEGER NOT NULL,
                confidence REAL NOT NULL,
                bbox_json TEXT NOT NULL,
                track_id INTEGER NOT NULL
            );
            CREATE TABLE evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL
            );
            CREATE TABLE reviews (
                review_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_status TEXT NOT NULL,
                explanation TEXT NOT NULL,
                evidence_ids_json TEXT NOT NULL,
                claims_json TEXT NOT NULL,
                created_ms INTEGER NOT NULL
            );
            CREATE TABLE review_results (
                event_id TEXT PRIMARY KEY,
                verdict TEXT,
                confidence REAL,
                evidence_status TEXT,
                explanation TEXT,
                parsed_at_ms INTEGER NOT NULL
            );
            CREATE TABLE durable_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_revision INTEGER NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                available_at_ms INTEGER NOT NULL,
                created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL
            );
            CREATE TABLE unrelated (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO events VALUES ('event-1', 'camera-1', 1000, 1, 'pending', 1000)"
        )
        connection.execute(
            "INSERT INTO detections VALUES (NULL, 'event-1', 'person', 0, 0.9, '[]', 1)"
        )
        connection.execute(
            "INSERT INTO evidence VALUES (NULL, 'event-1', 'frame', '/tmp/frame.jpg')"
        )
        connection.execute(
            "INSERT INTO reviews VALUES "
            "('review-1', 'event-1', 0, 'allow', 0.9, 'available', 'ok', '[]', '[]', 1000)"
        )
        connection.execute(
            "INSERT INTO review_results VALUES ('event-1', 'allow', 0.9, 'available', 'ok', 1000)"
        )
        connection.execute(
            "INSERT INTO durable_jobs VALUES "
            "(NULL, 'review', 'event-1', 0, 'pending', 0, 1000, 1000, 1000)"
        )
        connection.execute("INSERT INTO unrelated (value) VALUES ('keep me')")
        connection.commit()
    finally:
        connection.close()


class EventDbAdminTest(unittest.TestCase):
    def test_status_reports_runtime_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            _create_event_db(path)

            status = EventDbAdmin(path).status()

        self.assertTrue(status.exists)
        self.assertEqual(status.events, 1)
        self.assertEqual(status.detections, 1)
        self.assertEqual(status.durable_jobs, 1)
        self.assertEqual(status.total_rows, 6)
        self.assertEqual(status.durable_job_states[0].expired_leases, 0)

    def test_status_breaks_down_durable_jobs_and_expired_leases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            _create_event_db(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("ALTER TABLE durable_jobs ADD COLUMN lease_expires_ms INTEGER")
                connection.execute(
                    "INSERT INTO durable_jobs "
                    "(kind, entity_id, entity_revision, state, attempts, available_at_ms, "
                    "lease_expires_ms, created_ms, updated_ms) "
                    "VALUES ('review', 'event-1', 1, 'pending', 0, 1000, NULL, 1000, 1500)"
                )
                connection.execute(
                    "UPDATE durable_jobs SET kind = 'index', state = 'processing', "
                    "attempts = 3, lease_expires_ms = 1, updated_ms = 2000 "
                    "WHERE job_id = 1"
                )
                connection.execute(
                    "INSERT INTO durable_jobs "
                    "(kind, entity_id, entity_revision, state, attempts, available_at_ms, "
                    "lease_expires_ms, created_ms, updated_ms) "
                    "VALUES ('index', 'event-1', 1, 'processing', 1, 1000, "
                    "9223372036854775807, 1000, 2500)"
                )
                connection.execute(
                    "INSERT INTO durable_jobs "
                    "(kind, entity_id, entity_revision, state, attempts, available_at_ms, "
                    "lease_expires_ms, created_ms, updated_ms) "
                    "VALUES ('review', 'event-1', 0, 'dead', 4, 1000, NULL, 1000, 3000)"
                )
                connection.commit()
            finally:
                connection.close()

            states = {
                (item.kind, item.state): item
                for item in EventDbAdmin(path).status().durable_job_states
            }

        processing = states[("index", "processing")]
        assert processing.count == 2
        assert processing.max_attempts == 3
        assert processing.oldest_updated_ms == 2000
        assert processing.expired_leases == 1
        assert states[("review", "pending")].count == 1
        assert states[("review", "dead")].max_attempts == 4


    def test_status_lists_five_recent_dead_letters_with_safe_error_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            _create_event_db(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("ALTER TABLE durable_jobs ADD COLUMN last_error TEXT")
                for revision in range(1, 8):
                    error_code = (
                        "review_failed" if revision == 7 else f"sensitive detail {revision}"
                    )
                    connection.execute(
                        "INSERT INTO durable_jobs "
                        "(kind, entity_id, entity_revision, state, attempts, available_at_ms, "
                        "created_ms, updated_ms, last_error) "
                        "VALUES ('review', ?, ?, 'dead', ?, 1000, 1000, ?, ?)",
                        (
                            f"sensitive-event-{revision}",
                            revision,
                            revision,
                            2000 + revision,
                            error_code,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

            status = EventDbAdmin(path).status()

        self.assertEqual(len(status.dead_letters), 5)
        self.assertEqual(
            [item.entity_revision for item in status.dead_letters],
            [7, 6, 5, 4, 3],
        )
        self.assertEqual(status.dead_letters[0].error_code, "review_failed")
        self.assertTrue(
            all(item.error_code == "legacy_unclassified" for item in status.dead_letters[1:])
        )
        self.assertFalse(hasattr(status.dead_letters[0], "entity_id"))
        self.assertFalse(hasattr(status.dead_letters[0], "last_error"))

    def test_status_without_last_error_column_uses_legacy_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            _create_event_db(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE durable_jobs SET state = 'dead', updated_ms = 2000"
                )
                connection.commit()
            finally:
                connection.close()

            status = EventDbAdmin(path).status()

        self.assertEqual(len(status.dead_letters), 1)
        self.assertEqual(status.dead_letters[0].error_code, "legacy_unclassified")

    def test_dry_run_does_not_delete_runtime_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            _create_event_db(path)

            result = EventDbAdmin(path).reset(dry_run=True)
            status = EventDbAdmin(path).status()

        self.assertTrue(result.dry_run)
        self.assertEqual(result.deleted_rows, 0)
        self.assertEqual(result.status_before.total_rows, 6)
        self.assertEqual(status.total_rows, 6)

    def test_clear_deletes_runtime_tables_and_resets_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            _create_event_db(path)

            result = EventDbAdmin(path).reset()
            status = EventDbAdmin(path).status()
            connection = sqlite3.connect(path)
            try:
                sequences = connection.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
                unrelated = connection.execute("SELECT value FROM unrelated").fetchall()
            finally:
                connection.close()

        self.assertFalse(result.dry_run)
        self.assertEqual(result.deleted_rows, 6)
        self.assertEqual(status.total_rows, 0)
        self.assertEqual(sequences, [("unrelated", 1)])
        self.assertEqual(unrelated, [("keep me",)])

    def test_missing_database_is_an_empty_runtime_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"

            admin = EventDbAdmin(path)
            status = admin.status()
            result = admin.reset()

        self.assertFalse(status.exists)
        self.assertEqual(status.total_rows, 0)
        self.assertEqual(result.deleted_rows, 0)
        self.assertFalse(path.exists())

    def test_rejects_a_non_event_ledger_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "other.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE unrelated (id INTEGER)")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(EventDbError):
                EventDbAdmin(path).status()

    def test_rejects_same_named_database_with_incompatible_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lookalike.db"
            connection = sqlite3.connect(path)
            try:
                for table in _TABLES:
                    connection.execute(f"CREATE TABLE {table} (id INTEGER)")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(EventDbError):
                EventDbAdmin(path).status()


if __name__ == "__main__":
    unittest.main()
