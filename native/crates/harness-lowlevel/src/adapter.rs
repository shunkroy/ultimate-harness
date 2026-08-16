//! Safe platform adapters. Only one contained, justified unsafe shim
//! (libc sysconf for page size); everything else is safe code.

use crate::governor::Capability;

/// CPU feature flags available on this device (aarch64 and x86_64 focus).
///
/// Uses `std::arch::is_*_feature_detected!` where applicable and augments
/// with /proc/cpuinfo parse (armv8 "Features" / x86 "flags" lines) for
/// flags not covered by std. Pure read-only; no privileges required.
pub fn cpu_features() -> Vec<String> {
    let mut features: Vec<String> = Vec::new();
    #[cfg(target_arch = "aarch64")]
    {
        if std::arch::is_aarch64_feature_detected!("asimd") {
            features.push("asimd".to_string());
        }
        if std::arch::is_aarch64_feature_detected!("crc") {
            features.push("crc".to_string());
        }
        if std::arch::is_aarch64_feature_detected!("lse") {
            features.push("lse".to_string());
        }
        if std::arch::is_aarch64_feature_detected!("rdm") {
            features.push("rdm".to_string());
        }
    }
    #[cfg(target_arch = "x86_64")]
    {
        if std::arch::is_x86_feature_detected!("sse2") {
            features.push("sse2".to_string());
        }
        if std::arch::is_x86_feature_detected!("avx") {
            features.push("avx".to_string());
        }
        if std::arch::is_x86_feature_detected!("avx2") {
            features.push("avx2".to_string());
        }
    }
    // /proc/cpuinfo: armv8 "Features" line and x86 "flags" line.
    if let Ok(text) = std::fs::read_to_string("/proc/cpuinfo") {
        for line in text.lines() {
            let line = line.trim();
            for prefix in ["Features", "flags"] {
                if let Some(value) = line.strip_prefix(prefix) {
                    let value = value.trim_start_matches(':').trim();
                    for token in value.split_whitespace() {
                        let token = token.to_string();
                        if !features.contains(&token) {
                            features.push(token);
                        }
                    }
                }
            }
        }
    }
    features.sort();
    features.dedup();
    features
}

/// Byte order of the platform.
pub fn endianness() -> String {
    #[cfg(target_endian = "little")]
    {
        "little".to_string()
    }
    #[cfg(target_endian = "big")]
    {
        "big".to_string()
    }
}

/// OS page size in bytes (libc sysconf).
pub fn page_size() -> i64 {
    // SAFETY: sysconf(_SC_PAGESIZE) is a pure query: no pointers, no
    // retained state, no observable side effects beyond the syscall
    // result. The single, contained unsafe shim in this module.
    unsafe { libc::sysconf(libc::_SC_PAGESIZE) }
}

/// The capability this adapter fulfills.
pub fn capability_of(name: &str) -> Capability {
    match name {
        "cpu_features" => Capability::CpuFeatureProbe,
        "endianness" => Capability::EndiannessProbe,
        "page_size" => Capability::PageSizeProbe,
        _ => Capability::CpuFeatureProbe,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpu_features_probe_is_safe_and_readable() {
        let features = cpu_features();
        assert!(!features.is_empty(), "no cpu features reported: {features:?}");
        #[cfg(target_arch = "aarch64")]
        assert!(features.iter().any(|f| f == "asimd"), "asimd missing: {features:?}");
        #[cfg(target_arch = "x86_64")]
        assert!(features.iter().any(|f| f == "sse2"), "sse2 missing: {features:?}");
    }

    #[test]
    fn endianness_is_little_on_device_targets() {
        let endian = endianness();
        assert!(endian == "little" || endian == "big");
        #[cfg(target_endian = "little")]
        assert_eq!(endian, "little");
    }

    #[test]
    fn page_size_is_positive() {
        assert!(page_size() > 0);
    }
}