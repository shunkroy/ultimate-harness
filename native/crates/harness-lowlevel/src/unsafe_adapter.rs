//! DESIGNED-ONLY unsafe adapter paths.
//!
//! STATUS: DOCUMENTED / DESIGNED — NOT enabled by any default policy,
//! NOT called from the safe adapter surface, NOT part of any test
//! execution. The governor denies `RawCpuinfoMmap` under the default
//! policy, so this code cannot be reached through the sanctioned flow
//! unless an operator explicitly grants it in a custom policy.
//!
//! It exists to prove the boundary exists and to document what an
//! unsafe path would look like (and why it is not needed): every probe
//! in `adapter` is achievable with safe code, so an unsafe adapter is
//! strictly a liability — capability-governed denial is the defense.

/// DESIGNED-only: mmap /proc/cpuinfo and return its size in bytes.
///
/// # Safety
/// - `fd` must be a valid open descriptor.
/// - The mapping is unmapped before return (RAII via closure).
/// - Do NOT call this; it is a specification artifact. The safe
///   `adapter::cpu_features` does the same job without unsafe.
pub unsafe fn raw_cpuinfo_mmap_size(fd: i32) -> Result<usize, String> {
    // Design sketch (never executed):
    //   let len = libc::fstat(...).st_size;
    //   let ptr = libc::mmap(ptr::null_mut(), len, PROT_READ, MAP_PRIVATE, fd, 0);
    //   ... parse bytes at ptr ...
    //   libc::munmap(ptr, len);
    let _ = fd;
    Err("raw_cpuinfo_mmap is DESIGNED-ONLY and must not be called".to_string())
}