//! Governor — capability authorization + audit.
//!
//! The governor is the single decision point. It answers the question
//! "may this capability be exercised in this policy context?" and
//! records the answer in an append-only audit log.

use serde::Serialize;
use std::time::{SystemTime, UNIX_EPOCH};

/// Every operation that touches native/platform resources.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Capability {
    /// Read CPU feature flags (std::arch + /proc/cpuinfo).
    CpuFeatureProbe,
    /// Read byte order of the platform.
    EndiannessProbe,
    /// Read OS page size.
    PageSizeProbe,
    /// DESIGNED-only: raw mmap of /proc/cpuinfo. Not in the default
    /// policy. Any policy that grants it is explicitly opting into an
    /// unsafe path.
    RawCpuinfoMmap,
}

/// Which capabilities a caller is allowed to use.
#[derive(Debug, Clone)]
pub struct Policy {
    allowed: Vec<Capability>,
}

impl Policy {
    pub fn new(allowed: Vec<Capability>) -> Self {
        Self { allowed }
    }

    /// Default policy: the safe adapter surface only.
    pub fn default_safe() -> Self {
        Self {
            allowed: vec![
                Capability::CpuFeatureProbe,
                Capability::EndiannessProbe,
                Capability::PageSizeProbe,
            ],
        }
    }

    pub fn allows(&self, capability: &Capability) -> bool {
        self.allowed.contains(capability)
    }
}

/// One audit record. Append-only: the log only grows.
#[derive(Debug, Clone, Serialize)]
pub struct AuditEntry {
    pub ts_ms: u128,
    pub request: String,
    pub authorized: bool,
    pub reason: String,
}

/// A capability request, as presented by the caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Request {
    pub capability: Capability,
}

impl Request {
    pub fn new(capability: Capability) -> Self {
        Self { capability }
    }
}

/// Decision point between requests and adapters.
#[derive(Debug)]
pub struct Governor {
    policy: Policy,
    audit: Vec<AuditEntry>,
}

impl Governor {
    pub fn new(policy: Policy) -> Self {
        Self {
            policy,
            audit: Vec::new(),
        }
    }

    pub fn policy(&self) -> &Policy {
        &self.policy
    }

    /// Authorize a request. Every call is audited, including denials.
    pub fn authorize(&mut self, request: &Request) -> bool {
        let authorized = self.policy.allows(&request.capability);
        let reason = if authorized {
            "policy permits".to_string()
        } else {
            format!("policy denies {:?}", request.capability)
        };
        self.audit.push(AuditEntry {
            ts_ms: now_ms(),
            request: format!("{:?}", request.capability),
            authorized,
            reason,
        });
        authorized
    }

    /// Audit trail (all requests, in order).
    pub fn audit(&self) -> &[AuditEntry] {
        &self.audit
    }

    /// JSON export of the audit trail.
    pub fn audit_json(&self) -> String {
        serde_json::to_string_pretty(&self.audit).unwrap_or_else(|_| "[]".to_string())
    }
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_capabilities_are_authorized_under_default_policy() {
        let mut governor = Governor::new(Policy::default_safe());
        assert!(governor.authorize(&Request::new(Capability::CpuFeatureProbe)));
        assert!(governor.authorize(&Request::new(Capability::EndiannessProbe)));
        assert!(governor.authorize(&Request::new(Capability::PageSizeProbe)));
        assert_eq!(governor.audit().len(), 3);
        assert!(governor.audit().iter().all(|e| e.authorized));
    }

    #[test]
    fn unsafe_capability_is_denied_by_default_and_audited() {
        let mut governor = Governor::new(Policy::default_safe());
        assert!(!governor.authorize(&Request::new(Capability::RawCpuinfoMmap)));
        let entry = &governor.audit()[0];
        assert!(!entry.authorized);
        assert!(entry.reason.contains("denies"));
    }

    #[test]
    fn denials_and_grants_are_ordered_in_audit() {
        let mut governor = Governor::new(Policy::default_safe());
        governor.authorize(&Request::new(Capability::CpuFeatureProbe));
        governor.authorize(&Request::new(Capability::RawCpuinfoMmap));
        governor.authorize(&Request::new(Capability::PageSizeProbe));
        let entries = governor.audit();
        assert_eq!(entries.len(), 3);
        assert!(entries[0].authorized);
        assert!(!entries[1].authorized);
        assert!(entries[2].authorized);
    }

    #[test]
    fn audit_exports_as_json() {
        let mut governor = Governor::new(Policy::default_safe());
        governor.authorize(&Request::new(Capability::CpuFeatureProbe));
        governor.authorize(&Request::new(Capability::RawCpuinfoMmap));
        let json = governor.audit_json();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.as_array().unwrap().len(), 2);
        assert_eq!(parsed[0]["authorized"], true);
        assert_eq!(parsed[1]["authorized"], false);
    }
}