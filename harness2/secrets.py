"""Cross-platform provider secret storage (Windows DPAPI, POSIX private JSON)."""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from typing import Dict

from .security import atomic_write_bytes, atomic_write_json, read_private_json


class SecretStoreError(RuntimeError):
    pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("DPAPI is only available on Windows")
    source, source_buffer = _blob(data)
    destination = _DATA_BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(source), "Harness2", None, None, None, 1, ctypes.byref(destination))
    if not ok:
        raise SecretStoreError(f"DPAPI failed with Windows error {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)
        del source_buffer


def load(path: str, windows: bool = False) -> Dict[str, str]:
    if not windows:
        raw = read_private_json(path)
    else:
        try:
            with open(path, "rb") as fh:
                raw = json.loads(_dpapi(fh.read(), decrypt=True).decode("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, SecretStoreError) as exc:
            raise SecretStoreError(f"could not load Windows secret store: {exc}") from exc
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v} if isinstance(raw, dict) else {}


def save(path: str, values: Dict[str, str], windows: bool = False) -> None:
    safe = {str(k): str(v) for k, v in values.items() if isinstance(v, str) and v}
    if windows:
        atomic_write_bytes(path, _dpapi(json.dumps(safe, sort_keys=True).encode("utf-8")))
    else:
        atomic_write_json(path, safe)


def protect_bytes(value: bytes) -> bytes:
    return _dpapi(value)


def unprotect_bytes(value: bytes) -> bytes:
    return _dpapi(value, decrypt=True)
