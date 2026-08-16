//! Read-only status/session access to the canonical state database.
//!
//! Interoperates with the existing Python Harness by reading the SAME
//! state — it never duplicates, writes, migrates or re-creates state.

use crate::types::{Session, SessionState};
use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub enum StatusError {
    MissingDatabase(PathBuf),
    Open(String),
    Query(String),
    MigrationDrift(String),
}

impl std::fmt::Display for StatusError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StatusError::MissingDatabase(path) => {
                write!(f, "no harness database at {}", path.display())
            }
            StatusError::Open(err) => write!(f, "cannot open database: {}", err),
            StatusError::Query(err) => write!(f, "database query failed: {}", err),
            StatusError::MigrationDrift(err) => write!(f, "migration drift: {}", err),
        }
    }
}

impl std::error::Error for StatusError {}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct StateSnapshot {
    pub state_root: String,
    pub schema: u32,
    pub migration_history: Vec<u32>,
    pub sessions_count: u64,
    pub integrity_pins: Option<u64>,
}

fn open_readonly(state_root: &Path) -> Result<Connection, StatusError> {
    let db = state_root.join("harness.db");
    if !db.is_file() {
        return Err(StatusError::MissingDatabase(db));
    }
    Connection::open_with_flags(&db, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|err| StatusError::Open(err.to_string()))
}

fn migration_history(conn: &Connection) -> Result<Vec<u32>, StatusError> {
    let mut statement = conn
        .prepare("SELECT version FROM kernel_schema_migrations ORDER BY version")
        .map_err(|err| StatusError::Query(err.to_string()))?;
    let rows = statement
        .query_map([], |row| row.get::<_, i64>(0))
        .map_err(|err| StatusError::Query(err.to_string()))?;
    let mut history = Vec::new();
    for row in rows {
        history.push(row.map_err(|err| StatusError::Query(err.to_string()))? as u32);
    }
    let expected: Vec<u32> = (1..=history.len() as u32).collect();
    if history != expected {
        return Err(StatusError::MigrationDrift(format!(
            "migration history is not contiguous: {:?}",
            history
        )));
    }
    Ok(history)
}

/// Read the canonical state snapshot for a state root.
pub fn read_state(state_root: &Path) -> Result<StateSnapshot, StatusError> {
    let conn = open_readonly(state_root)?;
    let history = migration_history(&conn)?;
    let schema = history.last().copied().unwrap_or(0);
    let sessions_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM sessions", [], |row| row.get(0))
        .map_err(|err| StatusError::Query(err.to_string()))?;
    let integrity_path = state_root.join("integrity.json");
    let integrity_pins = if integrity_path.is_file() {
        match std::fs::read_to_string(&integrity_path) {
            Ok(text) => serde_json::from_str::<serde_json::Value>(&text)
                .ok()
                .and_then(|value| {
                    value
                        .get("artifacts")
                        .and_then(|artifacts| artifacts.as_object())
                        .map(|map| map.len() as u64)
                }),
            Err(_) => None,
        }
    } else {
        None
    };
    Ok(StateSnapshot {
        state_root: state_root.display().to_string(),
        schema,
        migration_history: history,
        sessions_count: sessions_count as u64,
        integrity_pins,
    })
}

/// List sessions (read-only), newest first.
pub fn list_sessions(state_root: &Path, limit: i64) -> Result<Vec<Session>, StatusError> {
    let conn = open_readonly(state_root)?;
    let mut statement = conn
        .prepare(
            "SELECT id, title, state, created_at, updated_at, metadata_json \
             FROM sessions ORDER BY created_at DESC LIMIT ?1",
        )
        .map_err(|err| StatusError::Query(err.to_string()))?;
    let rows = statement
        .query_map([limit], |row| {
            let state: String = row.get(2)?;
            let session_state = if state == "open" {
                SessionState::Open
            } else {
                SessionState::Closed
            };
            Ok(Session {
                id: row.get(0)?,
                title: row.get(1)?,
                state: session_state,
                created_at: row.get(3)?,
                updated_at: row.get(4)?,
                metadata_json: row.get(5)?,
            })
        })
        .map_err(|err| StatusError::Query(err.to_string()))?;
    let mut sessions = Vec::new();
    for row in rows {
        sessions.push(row.map_err(|err| StatusError::Query(err.to_string()))?);
    }
    Ok(sessions)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seed_test_root(path: &Path) {
        std::fs::create_dir_all(path).unwrap();
        let conn = rusqlite::Connection::open(path.join("harness.db")).unwrap();
        conn.execute_batch(
            "CREATE TABLE kernel_schema_migrations (version INTEGER PRIMARY KEY, \
             name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at REAL NOT NULL); \
             INSERT INTO kernel_schema_migrations VALUES (1,'typed_events','a',1.0); \
             INSERT INTO kernel_schema_migrations VALUES (2,'typed_tasks','b',1.0); \
             INSERT INTO kernel_schema_migrations VALUES (3,'provider_observations','c',1.0); \
             INSERT INTO kernel_schema_migrations VALUES (4,'immutable_payloads','d',1.0); \
             INSERT INTO kernel_schema_migrations VALUES (5,'harness_sessions','e',1.0); \
             CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', \
             state TEXT NOT NULL CHECK(state IN ('open','closed')), created_at REAL NOT NULL, \
             updated_at REAL NOT NULL, metadata_json TEXT NOT NULL); \
             INSERT INTO sessions VALUES ('test-1','probe','closed',1.0,2.0,'{}');",
        )
        .unwrap();
    }

    #[test]
    fn reads_contiguous_v5_schema_and_session_count() {
        let tmp = std::env::temp_dir().join(format!("h2-status-test-{}", std::process::id()));
        seed_test_root(&tmp);
        let snapshot = read_state(&tmp).unwrap();
        assert_eq!(snapshot.schema, 5);
        assert_eq!(snapshot.migration_history, vec![1, 2, 3, 4, 5]);
        assert_eq!(snapshot.sessions_count, 1);
        let sessions = list_sessions(&tmp, 10).unwrap();
        assert_eq!(sessions.len(), 1);
        assert_eq!(sessions[0].id, "test-1");
        assert_eq!(sessions[0].state, SessionState::Closed);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn non_contiguous_history_is_fail_closed() {
        let tmp = std::env::temp_dir().join(format!("h2-drift-test-{}", std::process::id()));
        std::fs::create_dir_all(&tmp).unwrap();
        let conn = rusqlite::Connection::open(tmp.join("harness.db")).unwrap();
        conn.execute_batch(
            "CREATE TABLE kernel_schema_migrations (version INTEGER PRIMARY KEY, \
             name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at REAL NOT NULL); \
             INSERT INTO kernel_schema_migrations VALUES (5,'harness_sessions','x',1.0);",
        )
        .unwrap();
        let result = read_state(&tmp);
        assert!(matches!(result, Err(StatusError::MigrationDrift(_))));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn missing_database_is_an_error() {
        let tmp = std::env::temp_dir().join(format!("h2-missing-test-{}", std::process::id()));
        let result = read_state(&tmp);
        assert!(matches!(result, Err(StatusError::MissingDatabase(_))));
    }
}