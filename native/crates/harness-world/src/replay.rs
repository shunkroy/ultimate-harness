//! Deterministic replay: re-run a recorded command sequence with the
//! recorded timestamps and reproduce byte-identical state and hashes.
//!
//! The chain formula includes created_at, so replay injects the exact
//! recorded clock. Wall-clock nondeterminism is preserved IN the history
//! (each event's created_at) and is therefore replayable by construction.
//! Replay also works for forked branches (the fork event is part of the
//! recorded timeline).

use crate::runtime::{ActionResult, SessionSnapshot, WorldSession, WorldSessionError};
use std::cell::RefCell;
use std::collections::VecDeque;
use std::path::Path;
use std::rc::Rc;

#[derive(Debug, Clone, PartialEq)]
pub struct ReplayCommand {
    pub utterance: String,
    /// recorded created_at of the corresponding event (from history)
    pub created_at: String,
}

#[derive(Debug)]
pub struct ReplayOutcome {
    pub snapshot: SessionSnapshot,
    pub actions: Vec<ActionResult>,
    /// event hashes in order (one per action with an event)
    pub hashes: Vec<String>,
}

impl ReplayOutcome {
    pub fn event_count(&self) -> usize {
        self.hashes.len()
    }
}

/// Replay a recorded command sequence against a FRESH store (or a
/// resumed one — replay is resumable by design) with the recorded clock.
///
/// One clock tick is consumed per act; a leading dummy tick covers the
/// branch-creation timestamp inside open (which is not part of the
/// chain). Branch forking consumes exactly one tick (genesis and record
/// share the recorded timestamp).
pub fn replay(
    package_path: &Path,
    state_root: &Path,
    world_id: &str,
    instance_id: &str,
    branch_id: &str,
    commands: &[ReplayCommand],
) -> Result<ReplayOutcome, WorldSessionError> {
    let mut times: VecDeque<String> = commands
        .iter()
        .map(|cmd| cmd.created_at.clone())
        .collect();
    times.push_front("0".to_string());
    let times: Rc<RefCell<VecDeque<String>>> = Rc::new(RefCell::new(times));
    let clock_times = times.clone();
    let clock = Rc::new(move || {
        clock_times
            .borrow_mut()
            .pop_front()
            .unwrap_or_else(|| "0".to_string())
    });
    let mut session = WorldSession::open_with_clock(
        package_path,
        state_root,
        world_id,
        instance_id,
        branch_id,
        clock,
    )?;
    let mut actions = Vec::with_capacity(commands.len());
    let mut hashes = Vec::new();
    for command in commands {
        let result = session.act(&command.utterance)?;
        if let Some(event) = &result.event {
            hashes.push(event.hash.clone());
        }
        actions.push(result);
    }
    let snapshot = session.snapshot()?;
    session.close()?;
    Ok(ReplayOutcome {
        snapshot,
        actions,
        hashes,
    })
}