//! Runtime discovery: report reality, never fake installed environments.

use crate::types::Capability;
use std::path::{Path, PathBuf};

pub fn probe_runtime(name: &str, binary: &str) -> Capability {
    let path = find_in_path(binary);
    match path {
        Some(path) => Capability {
            name: name.to_string(),
            status: crate::types::CapabilityStatus::DeviceVerified,
            detail: Some(format!("{} at {}", binary, path.display())),
        },
        None => Capability {
            name: name.to_string(),
            status: crate::types::CapabilityStatus::Planned,
            detail: Some(format!("{} not found on PATH", binary)),
        },
    }
}

fn find_in_path(binary: &str) -> Option<PathBuf> {
    let path_var = std::env::var("PATH").ok()?;
    for dir in path_var.split(':') {
        if dir.is_empty() {
            continue;
        }
        let candidate = Path::new(dir).join(binary);
        if candidate.is_file() && is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .map(|m| m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(_path: &Path) -> bool {
    true
}

/// All runtimes Harness currently cares about. Detection reports what is
/// actually installed on this device; nothing is assumed.
pub fn detect_runtimes() -> Vec<Capability> {
    let probes: &[(&str, &str)] = &[
        ("python", "python3"),
        ("node", "node"),
        ("java", "java"),
        ("rust", "rustc"),
        ("cargo", "cargo"),
        ("go", "go"),
        ("dotnet", "dotnet"),
        ("cc", "cc"),
        ("gcc", "gcc"),
        ("wasmtime", "wasmtime"),
        ("git", "git"),
    ];
    probes
        .iter()
        .map(|(name, binary)| probe_runtime(name, binary))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probe_returns_missing_for_impossible_binary() {
        let capability = probe_runtime("definitely-not-real", "definitely-not-real-xyz-123");
        assert_eq!(capability.name, "definitely-not-real");
        assert_ne!(capability.status, crate::types::CapabilityStatus::DeviceVerified);
    }

    #[test]
    fn detection_never_panics() {
        let runtimes = detect_runtimes();
        assert!(!runtimes.is_empty());
    }
}