"""SQLite state, circuit persistence and hash-chained metadata audit."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from .models import EngineResult, RunRequest, RoutingDecision
from .security import PRIVATE_FILE_MODE, ensure_private_dir, redact, task_hash


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL NOT NULL,
  task_hash TEXT NOT NULL, engine TEXT NOT NULL, agent TEXT, model TEXT, provider TEXT,
  status TEXT NOT NULL, exit_code INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
  session_id TEXT, error_code TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS circuits (
  key TEXT PRIMARY KEY, state TEXT NOT NULL, failures INTEGER NOT NULL,
  opened_at REAL, cooldown REAL NOT NULL, last_error TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event TEXT NOT NULL,
  subject TEXT NOT NULL, metadata_json TEXT NOT NULL, prev_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS migrations (
  name TEXT PRIMARY KEY, applied_at REAL NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  status TEXT NOT NULL, task_hash TEXT NOT NULL, payload_path TEXT NOT NULL,
  engine TEXT NOT NULL, agent TEXT, model TEXT, provider TEXT, timeout INTEGER NOT NULL,
  cwd TEXT, sensitive INTEGER NOT NULL, untrusted INTEGER NOT NULL,
  no_fallback INTEGER NOT NULL, retries INTEGER NOT NULL,
  attempt INTEGER NOT NULL, max_attempts INTEGER NOT NULL,
  next_run_at REAL NOT NULL, lease_until REAL, worker_pid INTEGER,
  result_run_id TEXT, error_code TEXT, error_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status,next_run_at,created_at);
"""


class Store:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        ensure_private_dir(os.path.dirname(self.path))
        self._init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init(self) -> None:
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
        try:
            os.chmod(self.path, PRIVATE_FILE_MODE)
        except OSError:
            pass
        for suffix in ("-wal", "-shm"):
            try:
                os.chmod(self.path + suffix, PRIVATE_FILE_MODE)
            except (FileNotFoundError, OSError):
                pass  # SQLite may remove WAL/SHM between close and chmod.

    def setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )

    def append_audit(self, event: str, subject: str, metadata: Dict[str, Any]) -> str:
        safe = {str(k): redact(v, 300) for k, v in metadata.items()}
        payload = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = time.time()
        with self.connect() as con:
            row = con.execute("SELECT entry_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = str(row[0]) if row else "0" * 64
            body = f"{now:.6f}|{event}|{subject}|{payload}|{prev}"
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            con.execute(
                "INSERT INTO audit(ts,event,subject,metadata_json,prev_hash,entry_hash) VALUES(?,?,?,?,?,?)",
                (now, event, subject, payload, prev, digest),
            )
        return digest

    def verify_audit(self) -> tuple[bool, int, Optional[int]]:
        previous = "0" * 64
        count = 0
        with self.connect() as con:
            rows = con.execute("SELECT * FROM audit ORDER BY seq").fetchall()
        for row in rows:
            count += 1
            body = f"{float(row['ts']):.6f}|{row['event']}|{row['subject']}|{row['metadata_json']}|{previous}"
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if row["prev_hash"] != previous or row["entry_hash"] != digest:
                return False, count, int(row["seq"])
            previous = digest
        return True, count, None

    def record_run(self, request: RunRequest, decision: RoutingDecision, result: EngineResult, started: float) -> str:
        run_id = uuid.uuid4().hex
        finished = time.time()
        detail = redact(result.error or "", 300)
        with self.connect() as con:
            con.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, started, finished, task_hash(request.prompt), result.engine,
                    decision.agent, decision.model, request.provider,
                    "succeeded" if result.success else "failed", result.exit_code,
                    int(result.duration * 1000), result.session_id, result.error_code, detail,
                ),
            )
        self.append_audit(
            "run.completed", run_id,
            {
                "task_hash": task_hash(request.prompt), "engine": result.engine,
                "agent": decision.agent or "", "model": decision.model or "",
                "status": "succeeded" if result.success else "failed",
                "exit_code": result.exit_code, "error_code": result.error_code or "",
            },
        )
        return run_id

    def list_runs(self, limit: int = 20) -> list[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (max(1, limit),)).fetchall()
        return [dict(row) for row in rows]

    def circuit(self, key: str) -> Dict[str, Any]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM circuits WHERE key=?", (key,)).fetchone()
        return dict(row) if row else {
            "key": key, "state": "closed", "failures": 0, "opened_at": None,
            "cooldown": 30.0, "last_error": None, "updated_at": 0.0,
        }

    def save_circuit(self, value: Dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO circuits VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "state=excluded.state,failures=excluded.failures,opened_at=excluded.opened_at,"
                "cooldown=excluded.cooldown,last_error=excluded.last_error,updated_at=excluded.updated_at",
                (
                    value["key"], value["state"], int(value["failures"]), value.get("opened_at"),
                    float(value["cooldown"]), redact(value.get("last_error") or "", 200), time.time(),
                ),
            )

    def migration_applied(self, name: str) -> bool:
        with self.connect() as con:
            return con.execute("SELECT 1 FROM migrations WHERE name=?", (name,)).fetchone() is not None

    def mark_migration(self, name: str, detail: str = "") -> None:
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO migrations VALUES(?,?,?)", (name, time.time(), redact(detail)))
