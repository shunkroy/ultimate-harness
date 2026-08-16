//! harness-ffi — small, stable C ABI over the world runtime.
//!
//! Boundary rules:
//! - Every entry point catches panics (no unwind across the ABI).
//! - Opaque handles (u64) — no raw structs across the boundary.
//! - Strings returned are heap-allocated and freed via harness_string_free.
//! - Error reporting: i32 return codes (0 = ok, -1 = error) + a
//!   per-thread last-error string via harness_last_error().
//! - The ABI is the contract for future consumers: Kotlin/Swift/C/C++/
//!   Zig/WASM/desktop GUI/CLI/TUI/game engines. Internal Rust APIs are
//!   NOT exposed here; this surface grows only by deliberate design.
//!
//! ABI version: 1 (semver on the exported symbol set).

use std::ffi::{CStr, CString, c_char};
use std::path::Path;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::ptr;
use std::sync::Mutex;

use harness_world::runtime::WorldSession;

/// Return codes shared by every entry point.
pub const HARNESS_OK: i32 = 0;
pub const HARNESS_ERR: i32 = -1;

thread_local! {
    static LAST_ERROR: Mutex<Option<CString>> = Mutex::new(None);
}

/// Registry of live session handles. A handle is only ever dereferenced
/// while registered — stale/forged handles fail cleanly instead of
/// producing use-after-free or double-free across the ABI.
static LIVE_HANDLES: std::sync::LazyLock<Mutex<std::collections::HashSet<u64>>> =
    std::sync::LazyLock::new(|| Mutex::new(std::collections::HashSet::new()));

fn handle_valid(handle: u64) -> bool {
    handle != 0
        && LIVE_HANDLES
            .lock()
            .map(|guard| guard.contains(&handle))
            .unwrap_or(false)
}

fn handle_register(handle: u64) {
    if let Ok(mut guard) = LIVE_HANDLES.lock() {
        guard.insert(handle);
    }
}

fn handle_unregister(handle: u64) {
    if let Ok(mut guard) = LIVE_HANDLES.lock() {
        guard.remove(&handle);
    }
}

fn set_last_error(message: &str) {
    LAST_ERROR.with(|slot| {
        if let Ok(mut guard) = slot.lock() {
            *guard = Some(CString::new(message).unwrap_or_default());
        }
    });
}

fn clear_last_error() {
    LAST_ERROR.with(|slot| {
        if let Ok(mut guard) = slot.lock() {
            *guard = None;
        }
    });
}

fn capture<F: FnOnce() -> i32>(f: F) -> i32 {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(code) => code,
        Err(_) => {
            set_last_error("panic crossed the FFI boundary");
            HARNESS_ERR
        }
    }
}

unsafe fn cstr_to_string(ptr: *const c_char) -> Result<String, ()> {
    if ptr.is_null() {
        return Err(());
    }
    unsafe { CStr::from_ptr(ptr) }
        .to_str()
        .map(|s| s.to_string())
        .map_err(|_| ())
}

/// Pointer-safe string allocation for cross-boundary results.
/// Caller MUST free with harness_string_free.
unsafe fn string_out(handle: *mut *mut c_char, value: &str) -> i32 {
    let c = match CString::new(value) {
        Ok(c) => c,
        Err(_) => {
            set_last_error("string contains interior NUL");
            return HARNESS_ERR;
        }
    };
    unsafe {
        *handle = c.into_raw();
    }
    HARNESS_OK
}

// ---------------------------------------------------------------------
// Version / identity
// ---------------------------------------------------------------------

/// Stable ABI version. Bump only on breaking ABI changes.
#[unsafe(no_mangle)]
pub extern "C" fn harness_abi_version() -> u32 {
    1
}

/// Human-readable library version.
#[unsafe(no_mangle)]
pub extern "C" fn harness_version() -> *mut c_char {
    let version = format!(
        "harness-ffi {} (abi {})",
        env!("CARGO_PKG_VERSION"),
        harness_abi_version()
    );
    CString::new(version).unwrap_or_default().into_raw()
}

/// Last error message for the calling thread ("" when none).
/// Caller MUST free with harness_string_free.
#[unsafe(no_mangle)]
pub extern "C" fn harness_last_error() -> *mut c_char {
    LAST_ERROR.with(|slot| match slot.lock() {
        Ok(guard) => match guard.as_ref() {
            Some(c) => c.clone().into_raw(),
            None => CString::new("").unwrap_or_default().into_raw(),
        },
        Err(_) => CString::new("last-error lock poisoned").unwrap_or_default().into_raw(),
    })
}

#[unsafe(no_mangle)]
pub extern "C" fn harness_string_free(ptr: *mut c_char) {
    if !ptr.is_null() {
        unsafe {
            drop(CString::from_raw(ptr));
        }
    }
}

// ---------------------------------------------------------------------
// Compile
// ---------------------------------------------------------------------

/// Compile a world source into a .hdoor package directory.
#[unsafe(no_mangle)]
pub extern "C" fn harness_world_compile(
    source_path: *const c_char,
    world_id: *const c_char,
    title: *const c_char,
    out_dir: *const c_char,
) -> i32 {
    capture(|| {
        clear_last_error();
        let source_path = match unsafe { cstr_to_string(source_path) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid source_path");
                return HARNESS_ERR;
            }
        };
        let world_id = match unsafe { cstr_to_string(world_id) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid world_id");
                return HARNESS_ERR;
            }
        };
        let title = match unsafe { cstr_to_string(title) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid title");
                return HARNESS_ERR;
            }
        };
        let out_dir = match unsafe { cstr_to_string(out_dir) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid out_dir");
                return HARNESS_ERR;
            }
        };
        let source_text = match std::fs::read_to_string(Path::new(&source_path)) {
            Ok(text) => text,
            Err(err) => {
                set_last_error(&format!("read source: {err}"));
                return HARNESS_ERR;
            }
        };
        let world = harness_world::Compiler::new()
            .compile(&world_id, &title, &source_path, &source_text);
        match harness_world::write_package(Path::new(&out_dir), &world) {
            Ok(()) => HARNESS_OK,
            Err(err) => {
                set_last_error(&format!("write package: {err}"));
                HARNESS_ERR
            }
        }
    })
}

// ---------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------

/// Open (or resume) a world session. handle_out receives an opaque
/// handle; must be closed with harness_world_close.
#[unsafe(no_mangle)]
pub extern "C" fn harness_world_open(
    package_dir: *const c_char,
    state_root: *const c_char,
    instance_id: *const c_char,
    branch_id: *const c_char,
    handle_out: *mut u64,
) -> i32 {
    capture(|| {
        clear_last_error();
        if handle_out.is_null() {
            set_last_error("handle_out is null");
            return HARNESS_ERR;
        }
        let package_dir = match unsafe { cstr_to_string(package_dir) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid package_dir");
                return HARNESS_ERR;
            }
        };
        let state_root = match unsafe { cstr_to_string(state_root) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid state_root");
                return HARNESS_ERR;
            }
        };
        let instance_id = match unsafe { cstr_to_string(instance_id) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid instance_id");
                return HARNESS_ERR;
            }
        };
        let branch_id = match unsafe { cstr_to_string(branch_id) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid branch_id");
                return HARNESS_ERR;
            }
        };
        let world_id = match harness_world::read_package(Path::new(&package_dir)) {
            Ok(package) => package.manifest.world_id,
            Err(err) => {
                set_last_error(&format!("read package: {err}"));
                return HARNESS_ERR;
            }
        };
        match WorldSession::open(
            Path::new(&package_dir),
            Path::new(&state_root),
            &world_id,
            &instance_id,
            &branch_id,
        ) {
            Ok(session) => {
                unsafe {
                    *handle_out = Box::into_raw(Box::new(session)) as u64;
                }
                let registered = unsafe { *handle_out };
                handle_register(registered);
                HARNESS_OK
            }
            Err(err) => {
                set_last_error(&format!("open session: {err}"));
                HARNESS_ERR
            }
        }
    })
}

fn handle_session(handle: u64) -> Result<*mut WorldSession, ()> {
    if !handle_valid(handle) {
        return Err(());
    }
    Ok(handle as *mut WorldSession)
}

/// Run one utterance. result_out receives JSON
/// {"ok":true,"text":...,"event":{...}} or {"ok":false,"error":"..."}.
/// Caller MUST free result_out with harness_string_free.
#[unsafe(no_mangle)]
pub extern "C" fn harness_world_act(
    handle: u64,
    utterance: *const c_char,
    result_out: *mut *mut c_char,
) -> i32 {
    capture(|| {
        clear_last_error();
        let session = match handle_session(handle) {
            Ok(session) => unsafe { &mut *session },
            Err(_) => {
                set_last_error("invalid handle");
                return HARNESS_ERR;
            }
        };
        let utterance = match unsafe { cstr_to_string(utterance) } {
            Ok(s) => s,
            Err(_) => {
                set_last_error("invalid utterance");
                return HARNESS_ERR;
            }
        };
        let payload = match session.act(&utterance) {
            Ok(result) => serde_json::json!({
                "ok": true,
                "text": result.text,
                "event": result.event,
            }),
            Err(err) => serde_json::json!({
                "ok": false,
                "error": err.to_string(),
            }),
        };
        unsafe { string_out(result_out, &payload.to_string()) }
    })
}

/// Export the branch history as signed JSON. Caller MUST free
/// result_out with harness_string_free.
#[unsafe(no_mangle)]
pub extern "C" fn harness_world_export_json(
    handle: u64,
    result_out: *mut *mut c_char,
) -> i32 {
    capture(|| {
        clear_last_error();
        let session = match handle_session(handle) {
            Ok(session) => unsafe { &*session },
            Err(_) => {
                set_last_error("invalid handle");
                return HARNESS_ERR;
            }
        };
        match session.export() {
            Ok(export) => match serde_json::to_string_pretty(&export) {
                Ok(json) => unsafe { string_out(result_out, &json) },
                Err(err) => {
                    set_last_error(&format!("serialize export: {err}"));
                    HARNESS_ERR
                }
            },
            Err(err) => {
                set_last_error(&format!("export: {err}"));
                HARNESS_ERR
            }
        }
    })
}

/// Close the session (persist + chain verify). The handle is consumed
/// and must not be used again.
#[unsafe(no_mangle)]
pub extern "C" fn harness_world_close(handle: u64) -> i32 {
    capture(|| {
        clear_last_error();
        if !handle_valid(handle) {
            set_last_error("invalid handle");
            return HARNESS_ERR;
        }
        handle_unregister(handle);
        let session = unsafe { Box::from_raw(handle as *mut WorldSession) };
        match session.close() {
            Ok(()) => HARNESS_OK,
            Err(err) => {
                set_last_error(&format!("close session: {err}"));
                HARNESS_ERR
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    const FIXTURE: &str = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";

    fn c(s: &str) -> *const c_char {
        CString::new(s).unwrap().into_raw()
    }
    fn c_free(p: *const c_char) {
        if !p.is_null() {
            unsafe {
                drop(CString::from_raw(p as *mut c_char));
            }
        }
    }
    fn read_out(out: *mut *mut c_char) -> String {
        unsafe {
            let s = CStr::from_ptr(*out).to_str().unwrap().to_string();
            drop(CString::from_raw(*out));
            s
        }
    }

    #[test]
    fn abi_version_is_stable() {
        assert_eq!(harness_abi_version(), 1);
        let v = harness_version();
        let text = unsafe { CStr::from_ptr(v).to_str().unwrap().to_string() };
        c_free(v);
        assert!(text.starts_with("harness-ffi"));
    }

    #[test]
    fn compile_open_act_export_close_roundtrip() {
        let base = std::env::temp_dir().join(format!("ffi-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let source_path = base.join("world.txt");
        let out_dir = base.join("package");
        let state_root = base.join("state");
        std::fs::create_dir_all(&base).unwrap();
        std::fs::write(&source_path, FIXTURE).unwrap();

        let code = harness_world_compile(
            c(source_path.to_str().unwrap()),
            c("ffi-world"),
            c("FFI World"),
            c(out_dir.to_str().unwrap()),
        );
        assert_eq!(code, HARNESS_OK, "{}", unsafe {
            CStr::from_ptr(harness_last_error()).to_str().unwrap()
        });

        let mut handle: u64 = 0;
        let code = harness_world_open(
            c(out_dir.to_str().unwrap()),
            c(state_root.to_str().unwrap()),
            c("i1"),
            c("main"),
            &mut handle,
        );
        assert_eq!(code, HARNESS_OK, "{}", unsafe {
            CStr::from_ptr(harness_last_error()).to_str().unwrap()
        });
        assert_ne!(handle, 0);

        // act: talk to Sarn about the bronze key
        let mut result: *mut c_char = ptr::null_mut();
        let code = harness_world_act(handle, c("ask keeper sarn about the bronze key"), &mut result);
        assert_eq!(code, HARNESS_OK);
        let json = read_out(&mut result);
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["ok"], true);
        assert!(parsed["text"].as_str().unwrap().contains("Sarn"));

        // export + verify shape
        let mut export: *mut c_char = ptr::null_mut();
        let code = harness_world_export_json(handle, &mut export);
        assert_eq!(code, HARNESS_OK);
        let json = read_out(&mut export);
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["schema"], "hdoor_export_v1");
        assert!(parsed["events"].as_array().unwrap().len() >= 1);

        // close
        let code = harness_world_close(handle);
        assert_eq!(code, HARNESS_OK);

        // closed handle must fail cleanly, not crash
        let code = harness_world_close(handle);
        assert_eq!(code, HARNESS_ERR);

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn panic_does_not_cross_the_boundary() {
        // a zero handle on a consuming call returns an error, no unwind
        let mut result: *mut c_char = ptr::null_mut();
        let code = harness_world_act(0, c("status"), &mut result);
        assert_eq!(code, HARNESS_ERR);
        let err = harness_last_error();
        let text = unsafe { CStr::from_ptr(err).to_str().unwrap().to_string() };
        c_free(err);
        assert!(text.contains("invalid handle"));
    }
}