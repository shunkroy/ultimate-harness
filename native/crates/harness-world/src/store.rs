//! World store: SQLite-backed signed branch/event ledger.
//!
//! Every event is chained: hash = sha256(prev_hash ‖ seq ‖ branch_id ‖
//! event_type ‖ actor ‖ detail_json ‖ created_at). Genesis prev_hash is
//! bound to the canon manifest hash, so canon drift is detectable. The
//! chain is verifiable on demand — tampering with any event breaks every
//! subsequent hash.

use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub enum WorldStoreError {
    Io(String),
    Sqlite(String),
    BranchNotFound(String),
    ChainBroken { branch: String, seq: i64 },
    EmptyChain(String),
}

impl std::fmt::Display for WorldStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WorldStoreError::Io(message) => write!(f, "store io: {}", message),
            WorldStoreError::Sqlite(message) => write!(f, "store sqlite: {}", message),
            WorldStoreError::BranchNotFound(branch) => write!(f, "branch '{}' not found", branch),
            WorldStoreError::ChainBroken { branch, seq } => {
                write!(f, "event chain broken at {} seq {}", branch, seq)
            }
            WorldStoreError::EmptyChain(branch) => write!(f, "branch '{}' has no events", branch),
        }
    }
}

impl std::error::Error for WorldStoreError {}

impl From<rusqlite::Error> for WorldStoreError {
    fn from(err: rusqlite::Error) -> Self {
        WorldStoreError::Sqlite(err.to_string())
    }
}

pub struct StoredEvent {
    pub seq: i64,
    pub branch_id: String,
    pub event_type: String,
    pub actor: String,
    pub detail_json: String,
    pub created_at: String,
    pub prev_hash: String,
    pub hash: String,
}

/// Result of forking a branch: where the fork diverged from.
pub struct ForkInfo {
    pub parent: String,
    pub fork_seq: i64,
    pub fork_hash: String,
}

pub struct WorldStore {
    conn: Connection,
    path: PathBuf,
}

impl WorldStore {
    /// Open (creating if needed) a world store for `world_id/instance_id`.
    pub fn open(state_root: &Path, world_id: &str, instance_id: &str) -> Result<Self, WorldStoreError> {
        let dir = state_root.join("worlds").join(world_id).join(instance_id);
        std::fs::create_dir_all(&dir).map_err(|err| WorldStoreError::Io(err.to_string()))?;
        let path = dir.join("world.db");
        let conn = Connection::open(&path).map_err(WorldStoreError::from)?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA foreign_keys=ON;
             CREATE TABLE IF NOT EXISTS meta (
                 k TEXT PRIMARY KEY,
                 v TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS branches (
                 id TEXT PRIMARY KEY,
                 name TEXT NOT NULL,
                 base_branch TEXT,
                 created_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS events (
                 seq INTEGER NOT NULL,
                 branch_id TEXT NOT NULL,
                 event_type TEXT NOT NULL,
                 actor TEXT NOT NULL,
                 detail_json TEXT NOT NULL,
                 created_at TEXT NOT NULL,
                 prev_hash TEXT NOT NULL,
                 hash TEXT NOT NULL,
                 PRIMARY KEY (branch_id, seq)
             );
             CREATE TABLE IF NOT EXISTS kv (
                 branch_id TEXT NOT NULL,
                 k TEXT NOT NULL,
                 v TEXT NOT NULL,
                 PRIMARY KEY (branch_id, k)
             );",
        )
        .map_err(WorldStoreError::from)?;
        Ok(WorldStore { conn, path })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn meta(&self, key: &str) -> Result<Option<String>, WorldStoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT v FROM meta WHERE k = ?1")
            .map_err(WorldStoreError::from)?;
        let mut rows = stmt
            .query_map(params![key], |row| row.get::<_, String>(0))
            .map_err(WorldStoreError::from)?;
        rows.next()
            .transpose()
            .map_err(WorldStoreError::from)
    }

    pub fn set_meta(&self, key: &str, value: &str) -> Result<(), WorldStoreError> {
        self.conn
            .execute(
                "INSERT INTO meta (k, v) VALUES (?1, ?2)
                 ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                params![key, value],
            )
            .map_err(WorldStoreError::from)?;
        Ok(())
    }

    /// Bind the store to a canon manifest hash (genesis anchor).
    pub fn bind_canon(&self, manifest_hash: &str) -> Result<(), WorldStoreError> {
        self.set_meta("canon_manifest_hash", manifest_hash)
    }

    pub fn canon_hash(&self) -> Result<String, WorldStoreError> {
        self.meta("canon_manifest_hash")?.ok_or_else(|| {
            WorldStoreError::Io("store not bound to canon manifest".to_string())
        })
    }

    pub fn create_branch(
        &self,
        branch_id: &str,
        name: &str,
        base_branch: Option<&str>,
        created_at: &str,
    ) -> Result<(), WorldStoreError> {
        self.conn
            .execute(
                "INSERT OR IGNORE INTO branches (id, name, base_branch, created_at)
                 VALUES (?1, ?2, ?3, ?4)",
                params![branch_id, name, base_branch, created_at],
            )
            .map_err(WorldStoreError::from)?;
        Ok(())
    }

    pub fn branch_exists(&self, branch_id: &str) -> Result<bool, WorldStoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT 1 FROM branches WHERE id = ?1")
            .map_err(WorldStoreError::from)?;
        let mut rows = stmt
            .query_map(params![branch_id], |_| Ok(()))
            .map_err(WorldStoreError::from)?;
        Ok(rows.next().transpose().map_err(WorldStoreError::from)?.is_some())
    }

    /// The base branch a branch forked from, if any.
    pub fn base_branch(&self, branch_id: &str) -> Result<Option<String>, WorldStoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT base_branch FROM branches WHERE id = ?1")
            .map_err(WorldStoreError::from)?;
        let mut rows = stmt
            .query_map(params![branch_id], |row| row.get::<_, Option<String>>(0))
            .map_err(WorldStoreError::from)?;
        rows.next()
            .transpose()
            .map_err(WorldStoreError::from)?
            .ok_or_else(|| WorldStoreError::BranchNotFound(branch_id.to_string()))
    }

    /// Total branch count (used by `world list`).
    pub fn branch_count(&self) -> Result<usize, WorldStoreError> {
        let count: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM branches", [], |row| row.get(0))
            .map_err(WorldStoreError::from)?;
        Ok(count as usize)
    }

    /// Total event count across all branches (used by `world list`).
    pub fn event_count(&self) -> Result<i64, WorldStoreError> {
        let count: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM events", [], |row| row.get(0))
            .map_err(WorldStoreError::from)?;
        Ok(count)
    }

    /// The genesis anchor for a branch: the canon manifest hash for
    /// normal branches, or the parent's last hash for forked branches.
    pub fn genesis_prev(&self, branch_id: &str) -> Result<String, WorldStoreError> {
        match self.meta(&format!("genesis_prev.{branch_id}"))? {
            Some(anchor) => Ok(anchor),
            None => self.canon_hash(),
        }
    }

    /// Fork a branch at the parent's current tip. The child branch's
    /// genesis event chains from the parent's last hash (provenance:
    /// branch_diverged), so the fork point is cryptographically pinned.
    pub fn fork_branch(
        &mut self,
        child_id: &str,
        child_name: &str,
        parent_branch: &str,
        created_at: &str,
    ) -> Result<ForkInfo, WorldStoreError> {
        if !self.branch_exists(parent_branch)? {
            return Err(WorldStoreError::BranchNotFound(parent_branch.to_string()));
        }
        if self.branch_exists(child_id)? {
            return Err(WorldStoreError::Io(format!(
                "branch '{}' already exists",
                child_id
            )));
        }
        let parent_events = self.events(parent_branch)?;
        let (fork_seq, fork_hash) = match parent_events.last() {
            Some(event) => (event.seq, event.hash.clone()),
            None => {
                return Err(WorldStoreError::EmptyChain(parent_branch.to_string()));
            }
        };
        self.create_branch(child_id, child_name, Some(parent_branch), created_at)?;
        self.set_meta(&format!("genesis_prev.{child_id}"), &fork_hash)?;
        let detail = serde_json::json!({
            "from_branch": parent_branch,
            "from_seq": fork_seq,
            "provenance": "branch_diverged",
        });
        let detail_json = detail.to_string();
        let seq: i64 = 1;
        let genesis_hash = chain_hash(
            &fork_hash,
            seq,
            child_id,
            "branch_fork",
            "runtime",
            &detail_json,
            created_at,
        );
        let tx = self.conn.transaction().map_err(WorldStoreError::from)?;
        tx.execute(
            "INSERT INTO events (seq, branch_id, event_type, actor, detail_json, created_at, prev_hash, hash)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                seq,
                child_id,
                "branch_fork",
                "runtime",
                detail_json,
                created_at,
                fork_hash,
                genesis_hash
            ],
        )
        .map_err(WorldStoreError::from)?;
        tx.commit().map_err(WorldStoreError::from)?;
        Ok(ForkInfo {
            parent: parent_branch.to_string(),
            fork_seq,
            fork_hash,
        })
    }

    /// Copy all branch-scoped kv state from one branch to another
    /// (used when forking, so the child resumes the parent's state).
    pub fn kv_copy(&self, from_branch: &str, to_branch: &str) -> Result<(), WorldStoreError> {
        if !self.branch_exists(from_branch)? || !self.branch_exists(to_branch)? {
            return Err(WorldStoreError::BranchNotFound(format!(
                "{}/{}",
                from_branch, to_branch
            )));
        }
        self.conn
            .execute(
                "INSERT OR REPLACE INTO kv (branch_id, k, v)
                 SELECT ?1, k, v FROM kv WHERE branch_id = ?2",
                params![to_branch, from_branch],
            )
            .map_err(WorldStoreError::from)?;
        Ok(())
    }

    pub fn last_hash(&self, branch_id: &str) -> Result<String, WorldStoreError> {
        if !self.branch_exists(branch_id)? {
            return Err(WorldStoreError::BranchNotFound(branch_id.to_string()));
        }
        let mut stmt = self
            .conn
            .prepare(
                "SELECT hash FROM events WHERE branch_id = ?1 ORDER BY seq DESC LIMIT 1",
            )
            .map_err(WorldStoreError::from)?;
        let mut rows = stmt
            .query_map(params![branch_id], |row| row.get::<_, String>(0))
            .map_err(WorldStoreError::from)?;
        match rows.next().transpose().map_err(WorldStoreError::from)? {
            Some(hash) => Ok(hash),
            None => self.canon_hash().map_err(|_| {
                WorldStoreError::EmptyChain(branch_id.to_string())
            }),
        }
    }

    /// Append a signed event to a branch. Returns the chained hash.
    pub fn append_event(
        &mut self,
        branch_id: &str,
        event_type: &str,
        actor: &str,
        detail_json: &str,
        created_at: &str,
    ) -> Result<String, WorldStoreError> {
        if !self.branch_exists(branch_id)? {
            return Err(WorldStoreError::BranchNotFound(branch_id.to_string()));
        }
        let seq: i64 = self
            .conn
            .query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE branch_id = ?1",
                params![branch_id],
                |row| row.get(0),
            )
            .map_err(WorldStoreError::from)?;
        let prev_hash = self.last_hash(branch_id)?;
        let hash = chain_hash(&prev_hash, seq, branch_id, event_type, actor, detail_json, created_at);
        let tx = self.conn.transaction().map_err(WorldStoreError::from)?;
        tx.execute(
            "INSERT INTO events (seq, branch_id, event_type, actor, detail_json, created_at, prev_hash, hash)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![seq, branch_id, event_type, actor, detail_json, created_at, prev_hash, hash],
        )
        .map_err(WorldStoreError::from)?;
        tx.commit().map_err(WorldStoreError::from)?;
        Ok(hash)
    }

    pub fn events(&self, branch_id: &str) -> Result<Vec<StoredEvent>, WorldStoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT seq, branch_id, event_type, actor, detail_json, created_at, prev_hash, hash
                 FROM events WHERE branch_id = ?1 ORDER BY seq ASC",
            )
            .map_err(WorldStoreError::from)?;
        let rows = stmt
            .query_map(params![branch_id], |row| {
                Ok(StoredEvent {
                    seq: row.get(0)?,
                    branch_id: row.get(1)?,
                    event_type: row.get(2)?,
                    actor: row.get(3)?,
                    detail_json: row.get(4)?,
                    created_at: row.get(5)?,
                    prev_hash: row.get(6)?,
                    hash: row.get(7)?,
                })
            })
            .map_err(WorldStoreError::from)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(WorldStoreError::from)
    }

    /// Walk the whole chain and re-verify every hash. Returns event count.
    pub fn verify_chain(&self, branch_id: &str) -> Result<usize, WorldStoreError> {
        let events = self.events(branch_id)?;
        let mut expected_prev = self.genesis_prev(branch_id)?;
        for event in &events {
            let recomputed = chain_hash(
                &expected_prev,
                event.seq,
                &event.branch_id,
                &event.event_type,
                &event.actor,
                &event.detail_json,
                &event.created_at,
            );
            if recomputed != event.hash || event.prev_hash != expected_prev {
                return Err(WorldStoreError::ChainBroken {
                    branch: branch_id.to_string(),
                    seq: event.seq,
                });
            }
            expected_prev = event.hash.clone();
        }
        Ok(events.len())
    }

    pub fn kv_get(&self, branch_id: &str, key: &str) -> Result<Option<String>, WorldStoreError> {
        if !self.branch_exists(branch_id)? {
            return Err(WorldStoreError::BranchNotFound(branch_id.to_string()));
        }
        let mut stmt = self
            .conn
            .prepare("SELECT v FROM kv WHERE branch_id = ?1 AND k = ?2")
            .map_err(WorldStoreError::from)?;
        let mut rows = stmt
            .query_map(params![branch_id, key], |row| row.get::<_, String>(0))
            .map_err(WorldStoreError::from)?;
        rows.next()
            .transpose()
            .map_err(WorldStoreError::from)
    }

    pub fn kv_set(&self, branch_id: &str, key: &str, value: &str) -> Result<(), WorldStoreError> {
        if !self.branch_exists(branch_id)? {
            return Err(WorldStoreError::BranchNotFound(branch_id.to_string()));
        }
        self.conn
            .execute(
                "INSERT INTO kv (branch_id, k, v) VALUES (?1, ?2, ?3)
                 ON CONFLICT(branch_id, k) DO UPDATE SET v = excluded.v",
                params![branch_id, key, value],
            )
            .map_err(WorldStoreError::from)?;
        Ok(())
    }

    pub fn kv_all(&self, branch_id: &str) -> Result<Vec<(String, String)>, WorldStoreError> {
        if !self.branch_exists(branch_id)? {
            return Err(WorldStoreError::BranchNotFound(branch_id.to_string()));
        }
        let mut stmt = self
            .conn
            .prepare("SELECT k, v FROM kv WHERE branch_id = ?1 ORDER BY k")
            .map_err(WorldStoreError::from)?;
        let rows = stmt
            .query_map(params![branch_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(WorldStoreError::from)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(WorldStoreError::from)
    }
}

pub fn chain_hash(
    prev_hash: &str,
    seq: i64,
    branch_id: &str,
    event_type: &str,
    actor: &str,
    detail_json: &str,
    created_at: &str,
) -> String {
    let mut hasher = Sha256::new();
    hasher.update(prev_hash.as_bytes());
    hasher.update(b"|");
    hasher.update(seq.to_string().as_bytes());
    hasher.update(b"|");
    hasher.update(branch_id.as_bytes());
    hasher.update(b"|");
    hasher.update(event_type.as_bytes());
    hasher.update(b"|");
    hasher.update(actor.as_bytes());
    hasher.update(b"|");
    hasher.update(detail_json.as_bytes());
    hasher.update(b"|");
    hasher.update(created_at.as_bytes());
    format!("{:x}", hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("hdoor-store-{}-{}", tag, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn now() -> String {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis()
            .to_string()
    }

    #[test]
    fn events_chain_and_verify() {
        let root = temp_root("chain");
        let mut store = WorldStore::open(&root, "w1", "i1").unwrap();
        store.bind_canon("canon:abc123").unwrap();
        store
            .create_branch("main", "main", None, &now())
            .unwrap();
        let t0 = now();
        let h1 = store
            .append_event("main", "enter", "traveller", r#"{"loc":"gate"}"#, &t0)
            .unwrap();
        let t1 = now();
        let h2 = store
            .append_event("main", "take", "traveller", r#"{"item":"key"}"#, &t1)
            .unwrap();
        assert_ne!(h1, h2);
        assert_eq!(store.verify_chain("main").unwrap(), 2);
        assert_eq!(store.last_hash("main").unwrap(), h2);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn tampered_event_breaks_chain() {
        let root = temp_root("tamper");
        let mut store = WorldStore::open(&root, "w1", "i1").unwrap();
        store.bind_canon("canon:abc123").unwrap();
        store.create_branch("main", "main", None, &now()).unwrap();
        let t0 = now();
        store
            .append_event("main", "enter", "traveller", r#"{"loc":"gate"}"#, &t0)
            .unwrap();
        let t1 = now();
        store
            .append_event("main", "take", "traveller", r#"{"item":"key"}"#, &t1)
            .unwrap();
        // tamper with the first event's detail
        store
            .conn
            .execute(
                "UPDATE events SET detail_json = ?1 WHERE branch_id = 'main' AND seq = 1",
                params![r#"{"loc":"tampered"}"#],
            )
            .unwrap();
        let result = store.verify_chain("main");
        assert!(matches!(
            result,
            Err(WorldStoreError::ChainBroken { seq: 1, .. })
        ));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn branches_are_isolated() {
        let root = temp_root("branches");
        let mut store = WorldStore::open(&root, "w1", "i1").unwrap();
        store.bind_canon("canon:abc123").unwrap();
        store.create_branch("main", "main", None, &now()).unwrap();
        store.create_branch("alt", "alt", Some("main"), &now()).unwrap();
        let t0 = now();
        store
            .append_event("main", "move", "traveller", r#"{"to":"left"}"#, &t0)
            .unwrap();
        store
            .append_event("alt", "move", "traveller", r#"{"to":"right"}"#, &t0)
            .unwrap();
        assert_eq!(store.events("main").unwrap().len(), 1);
        assert_eq!(store.events("alt").unwrap().len(), 1);
        // both chains verify against the same canon anchor
        assert_eq!(store.verify_chain("main").unwrap(), 1);
        assert_eq!(store.verify_chain("alt").unwrap(), 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn kv_is_branch_scoped() {
        let root = temp_root("kv");
        let mut store = WorldStore::open(&root, "w1", "i1").unwrap();
        store.bind_canon("canon:abc123").unwrap();
        store.create_branch("main", "main", None, &now()).unwrap();
        store.create_branch("alt", "alt", Some("main"), &now()).unwrap();
        store.kv_set("main", "location", "gate").unwrap();
        store.kv_set("alt", "location", "well").unwrap();
        assert_eq!(store.kv_get("main", "location").unwrap().unwrap(), "gate");
        assert_eq!(store.kv_get("alt", "location").unwrap().unwrap(), "well");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn append_to_missing_branch_fails() {
        let root = temp_root("missing");
        let mut store = WorldStore::open(&root, "w1", "i1").unwrap();
        store.bind_canon("canon:abc123").unwrap();
        let result = store.append_event("nope", "enter", "traveller", "{}", &now());
        assert!(matches!(result, Err(WorldStoreError::BranchNotFound(_))));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn persistence_across_reopen() {
        let root = temp_root("persist");
        {
            let mut store = WorldStore::open(&root, "w1", "i1").unwrap();
            store.bind_canon("canon:abc123").unwrap();
            store.create_branch("main", "main", None, &now()).unwrap();
            let t0 = now();
            store
                .append_event("main", "enter", "traveller", r#"{"loc":"gate"}"#, &t0)
                .unwrap();
        }
        {
            let store = WorldStore::open(&root, "w1", "i1").unwrap();
            assert_eq!(store.verify_chain("main").unwrap(), 1);
            assert_eq!(store.canon_hash().unwrap(), "canon:abc123");
        }
        {
            let store = WorldStore::open(&root, "w1", "i1").unwrap();
            assert_eq!(store.verify_chain("main").unwrap(), 1);
            assert_eq!(store.canon_hash().unwrap(), "canon:abc123");
        }
        let _ = std::fs::remove_dir_all(&root);
    }
}