"""Ordered, additive SQLite migrations for provider-independent kernel state."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Sequence


class MigrationError(RuntimeError):
    """Base class for deterministic migration failures."""


class SchemaTooNewError(MigrationError):
    pass


class MigrationDriftError(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        canonical = "\n-- statement --\n".join(item.strip() for item in self.statements)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "typed_events", (
        """
        CREATE TABLE kernel_events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL,
          schema_version INTEGER NOT NULL CHECK(schema_version > 0),
          source TEXT NOT NULL,
          task_id TEXT,
          correlation_id TEXT NOT NULL,
          causation_id TEXT,
          occurred_at REAL NOT NULL,
          recorded_at REAL NOT NULL,
          payload_json TEXT NOT NULL,
          payload_sha256 TEXT NOT NULL,
          metadata_json TEXT NOT NULL,
          dedup_key TEXT,
          UNIQUE(source, dedup_key)
        )
        """,
        "CREATE INDEX idx_kernel_events_task_seq ON kernel_events(task_id, seq)",
        "CREATE INDEX idx_kernel_events_type_seq ON kernel_events(event_type, seq)",
        """
        CREATE TABLE kernel_event_consumers (
          consumer_id TEXT PRIMARY KEY,
          last_seq INTEGER NOT NULL DEFAULT 0 CHECK(last_seq >= 0),
          updated_at REAL NOT NULL
        )
        """,
    )),
    Migration(2, "typed_tasks", (
        """
        CREATE TABLE kernel_tasks (
          task_id TEXT PRIMARY KEY,
          schema_version INTEGER NOT NULL CHECK(schema_version > 0),
          task_type TEXT NOT NULL,
          state TEXT NOT NULL,
          source TEXT NOT NULL,
          reason TEXT NOT NULL,
          priority INTEGER NOT NULL,
          authority TEXT NOT NULL,
          objective_hash TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          idempotency_key TEXT UNIQUE,
          required_capabilities_json TEXT NOT NULL,
          constraints_json TEXT NOT NULL,
          budget_json TEXT NOT NULL,
          preferred_runtime TEXT,
          max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
          attempts_started INTEGER NOT NULL DEFAULT 0 CHECK(attempts_started >= 0),
          current_fence INTEGER NOT NULL DEFAULT 0 CHECK(current_fence >= 0),
          active_attempt_id TEXT,
          next_run_at REAL NOT NULL,
          result_hash TEXT,
          error_code TEXT,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          terminal_at REAL,
          revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)
        )
        """,
        "CREATE INDEX idx_kernel_tasks_claim ON kernel_tasks(state, next_run_at, priority DESC, created_at)",
        """
        CREATE TABLE kernel_task_attempts (
          attempt_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES kernel_tasks(task_id),
          attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
          fence_token INTEGER NOT NULL CHECK(fence_token > 0),
          state TEXT NOT NULL,
          owner_id TEXT NOT NULL,
          lease_until REAL NOT NULL,
          runtime_id TEXT NOT NULL,
          plan_id TEXT NOT NULL,
          plan_json TEXT NOT NULL,
          plan_sha256 TEXT NOT NULL,
          started_at REAL NOT NULL,
          renewed_at REAL NOT NULL,
          finished_at REAL,
          outcome_hash TEXT,
          error_code TEXT,
          UNIQUE(task_id, attempt_no),
          UNIQUE(task_id, fence_token)
        )
        """,
        "CREATE INDEX idx_kernel_attempts_expiry ON kernel_task_attempts(state, lease_until)",
        """
        CREATE TABLE kernel_task_transitions (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          transition_id TEXT NOT NULL UNIQUE,
          task_id TEXT NOT NULL REFERENCES kernel_tasks(task_id),
          attempt_id TEXT,
          from_state TEXT,
          to_state TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          event_id TEXT NOT NULL UNIQUE,
          occurred_at REAL NOT NULL,
          revision INTEGER NOT NULL
        )
        """,
        "CREATE INDEX idx_kernel_transitions_task_seq ON kernel_task_transitions(task_id, seq)",
    )),
    Migration(3, "provider_observations_and_resources", (
        """
        CREATE TABLE kernel_provider_observations (
          observation_id TEXT PRIMARY KEY,
          provider_id TEXT NOT NULL,
          runtime_id TEXT NOT NULL,
          capability_id TEXT NOT NULL,
          task_id TEXT,
          success INTEGER NOT NULL CHECK(success IN (0,1)),
          correctness_score REAL,
          latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
          estimated_cost REAL,
          failure_class TEXT,
          tool_use_score REAL,
          privacy_class TEXT NOT NULL,
          offline_available INTEGER NOT NULL CHECK(offline_available IN (0,1)),
          observed_at REAL NOT NULL,
          evidence_hash TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_kernel_provider_scores ON kernel_provider_observations(provider_id,capability_id,observed_at)",
        """
        CREATE TABLE kernel_resource_observations (
          observation_id TEXT PRIMARY KEY,
          node_id TEXT NOT NULL,
          observed_at REAL NOT NULL,
          cpu_load REAL,
          memory_available_bytes INTEGER,
          disk_free_bytes INTEGER,
          battery_percent REAL,
          charging INTEGER,
          thermal_state TEXT,
          network_available INTEGER,
          process_count INTEGER,
          queue_length INTEGER,
          payload_json TEXT NOT NULL,
          payload_sha256 TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_kernel_resources_node_time ON kernel_resource_observations(node_id,observed_at)",
    )),
    Migration(4, "immutable_payloads_checkpoints_and_snapshots", (
        """
        CREATE TABLE kernel_payload_references (
          reference_id TEXT PRIMARY KEY,
          backend_id TEXT NOT NULL,
          object_key TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
          media_type TEXT NOT NULL,
          schema_id TEXT NOT NULL,
          purpose TEXT NOT NULL,
          envelope_version INTEGER NOT NULL CHECK(envelope_version > 0),
          key_id TEXT NOT NULL,
          reference_mac TEXT NOT NULL,
          created_at REAL NOT NULL,
          UNIQUE(backend_id, object_key)
        )
        """,
        """
        CREATE TABLE kernel_task_payload_bindings (
          task_id TEXT NOT NULL REFERENCES kernel_tasks(task_id),
          role TEXT NOT NULL,
          reference_id TEXT NOT NULL REFERENCES kernel_payload_references(reference_id),
          bound_at REAL NOT NULL,
          PRIMARY KEY(task_id, role)
        )
        """,
        "CREATE INDEX idx_kernel_payload_bindings_ref ON kernel_task_payload_bindings(reference_id)",
        """
        CREATE TABLE kernel_task_checkpoints (
          checkpoint_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES kernel_tasks(task_id),
          attempt_id TEXT NOT NULL REFERENCES kernel_task_attempts(attempt_id),
          fence_token INTEGER NOT NULL CHECK(fence_token > 0),
          checkpoint_seq INTEGER NOT NULL CHECK(checkpoint_seq > 0),
          workflow_step TEXT NOT NULL,
          resource_versions_json TEXT NOT NULL,
          reference_id TEXT NOT NULL REFERENCES kernel_payload_references(reference_id),
          parent_checkpoint_hash TEXT NOT NULL,
          integrity_hash TEXT NOT NULL,
          resumable INTEGER NOT NULL CHECK(resumable IN (0,1)),
          schema_version INTEGER NOT NULL CHECK(schema_version > 0),
          created_at REAL NOT NULL,
          event_id TEXT NOT NULL UNIQUE REFERENCES kernel_events(event_id),
          UNIQUE(task_id, checkpoint_seq),
          UNIQUE(attempt_id, checkpoint_seq)
        )
        """,
        "CREATE INDEX idx_kernel_checkpoints_task_seq ON kernel_task_checkpoints(task_id,checkpoint_seq DESC)",
        """
        CREATE TABLE kernel_source_snapshots (
          snapshot_id TEXT PRIMARY KEY,
          reference_id TEXT NOT NULL UNIQUE REFERENCES kernel_payload_references(reference_id),
          source_type TEXT NOT NULL,
          source_identifier_hash TEXT NOT NULL,
          source_revision TEXT,
          content_sha256 TEXT NOT NULL,
          size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
          media_type TEXT NOT NULL,
          metadata_json TEXT NOT NULL,
          captured_at REAL NOT NULL
        )
        """,
        "CREATE INDEX idx_kernel_snapshots_content ON kernel_source_snapshots(content_sha256)",
        """
        CREATE TABLE kernel_task_type_snapshots (
          task_id TEXT PRIMARY KEY REFERENCES kernel_tasks(task_id),
          task_type TEXT NOT NULL,
          descriptor_version INTEGER NOT NULL CHECK(descriptor_version > 0),
          descriptor_sha256 TEXT NOT NULL,
          resource_requirements_json TEXT NOT NULL,
          side_effect_class TEXT NOT NULL,
          resumable INTEGER NOT NULL CHECK(resumable IN (0,1))
        )
        """,
        """
        CREATE TABLE kernel_legacy_job_tasks (
          job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
          task_id TEXT NOT NULL UNIQUE REFERENCES kernel_tasks(task_id),
          payload_reference_id TEXT NOT NULL REFERENCES kernel_payload_references(reference_id),
          latest_attempt_id TEXT,
          latest_fence_token INTEGER,
          projection_revision INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE kernel_provenance_observations (
          observation_id TEXT PRIMARY KEY,
          subject_kind TEXT NOT NULL,
          subject_id TEXT NOT NULL,
          subject_sha256 TEXT NOT NULL,
          producer_identity TEXT NOT NULL,
          verification_method TEXT NOT NULL,
          verification_status TEXT NOT NULL,
          signature_metadata_json TEXT NOT NULL,
          evidence_reference_id TEXT REFERENCES kernel_payload_references(reference_id),
          observed_at REAL NOT NULL,
          expires_at REAL,
          event_id TEXT UNIQUE REFERENCES kernel_events(event_id)
        )
        """,
        "CREATE INDEX idx_kernel_provenance_subject ON kernel_provenance_observations(subject_kind,subject_id,observed_at DESC)",
    )),
    Migration(5, "harness_sessions", (
        # Legacy runs.session_id keeps its historical meaning: the
        # provider/runtime session id reported by the engine. Harness session
        # identity is a separate, Harness-owned column; provider ids are
        # additionally mirrored into an explicit column so old rows can be
        # backfilled without reinterpreting their meaning.
        "ALTER TABLE runs ADD COLUMN harness_session_id TEXT",
        "ALTER TABLE runs ADD COLUMN provider_session_id TEXT",
        "UPDATE runs SET provider_session_id = session_id WHERE provider_session_id IS NULL AND session_id IS NOT NULL",
        """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL CHECK(state IN ('open','closed')),
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          metadata_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE session_turns (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL REFERENCES sessions(id),
          role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','error','summary')),
          content_envelope BLOB NOT NULL,
          envelope_version INTEGER NOT NULL CHECK(envelope_version > 0),
          key_id TEXT,
          status TEXT NOT NULL,
          engine TEXT,
          provider TEXT,
          model TEXT,
          provider_session_id TEXT,
          run_id TEXT,
          sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
          untrusted INTEGER NOT NULL DEFAULT 0 CHECK(untrusted IN (0,1)),
          error_code TEXT,
          duration_ms REAL,
          created_at REAL NOT NULL,
          summary_from_seq INTEGER,
          summary_to_seq INTEGER
        )
        """,
        "CREATE INDEX idx_session_turns_lookup ON session_turns(session_id, seq)",
        """
        CREATE TABLE session_attachments (
          session_id TEXT NOT NULL REFERENCES sessions(id),
          context_id TEXT NOT NULL,
          attached_at REAL NOT NULL,
          PRIMARY KEY(session_id, context_id)
        )
        """,
    )),
)


class Migrator:
    """Apply immutable migrations one transaction at a time."""

    def __init__(self, path: str, migrations: Sequence[Migration] = MIGRATIONS):
        self.path = path
        self.migrations = tuple(sorted(migrations, key=lambda item: item.version))
        expected = tuple(range(1, len(self.migrations) + 1))
        actual = tuple(item.version for item in self.migrations)
        if actual != expected:
            raise MigrationError(f"migration versions must be contiguous: {actual}")

    @property
    def latest_version(self) -> int:
        return self.migrations[-1].version if self.migrations else 0

    @staticmethod
    def _metadata(con: sqlite3.Connection) -> None:
        con.execute(
            "CREATE TABLE IF NOT EXISTS kernel_schema_migrations ("
            "version INTEGER PRIMARY KEY CHECK(version > 0),"
            "name TEXT NOT NULL UNIQUE,checksum TEXT NOT NULL,applied_at REAL NOT NULL)"
        )

    def verify(self, con: sqlite3.Connection) -> int:
        self._metadata(con)
        rows = con.execute(
            "SELECT version,name,checksum FROM kernel_schema_migrations ORDER BY version"
        ).fetchall()
        if rows and int(rows[-1][0]) > self.latest_version:
            raise SchemaTooNewError(
                f"database schema {rows[-1][0]} is newer than binary {self.latest_version}"
            )
        by_version = {item.version: item for item in self.migrations}
        actual_versions = [int(row[0]) for row in rows]
        if actual_versions != list(range(1, len(actual_versions) + 1)):
            raise MigrationDriftError(f"migration history is not contiguous: {actual_versions}")
        for version, name, checksum in rows:
            expected = by_version.get(int(version))
            if expected is None:
                raise SchemaTooNewError(f"unknown database schema version: {version}")
            if str(name) != expected.name or str(checksum) != expected.checksum:
                raise MigrationDriftError(f"migration drift at version {version}")
        return int(rows[-1][0]) if rows else 0

    def migrate(self, target: int | None = None) -> int:
        goal = self.latest_version if target is None else int(target)
        if goal < 0 or goal > self.latest_version:
            raise SchemaTooNewError(f"unsupported migration target: {goal}")
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("PRAGMA foreign_keys=ON")
            current = self.verify(con)
            for migration in self.migrations:
                if migration.version <= current or migration.version > goal:
                    continue
                con.execute("BEGIN IMMEDIATE")
                try:
                    current = self.verify(con)
                    if current >= migration.version:
                        con.commit()
                        continue
                    if current != migration.version - 1:
                        raise MigrationError(
                            f"expected schema {migration.version - 1}, found {current}"
                        )
                    for statement in migration.statements:
                        con.execute(statement)
                    con.execute(
                        "INSERT INTO kernel_schema_migrations(version,name,checksum,applied_at) "
                        "VALUES(?,?,?,?)",
                        (migration.version, migration.name, migration.checksum, time.time()),
                    )
                    con.commit()
                    current = migration.version
                except Exception:
                    con.rollback()
                    raise
            return current
        finally:
            con.close()
