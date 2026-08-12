"""Authenticated encrypted envelopes for durable job payloads.

Uses OpenSSL AES-256-CTR/PBKDF2 for confidentiality plus stdlib HMAC-SHA256
(encrypt-then-MAC). The passphrase is sent through a private inherited file
descriptor, never argv or environment; plaintext/ciphertext use stdin/stdout.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import tempfile
import shutil

from .security import PRIVATE_FILE_MODE, SecurityViolation, ensure_private_dir


MAGIC = b"H2J1"
MAC_SIZE = 32


class CryptoError(RuntimeError):
    pass


def load_or_create_key(path: str) -> bytes:
    path = os.path.abspath(path)
    ensure_private_dir(os.path.dirname(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, PRIVATE_FILE_MODE)
        key = os.urandom(32)
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(path, PRIVATE_FILE_MODE)
        except OSError:
            pass
        return key
    try:
        key = os.read(fd, 128)
    finally:
        os.close(fd)
    if len(key) != 32:
        raise CryptoError("job master key must be exactly 32 bytes")
    return key


def _derive(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, b"harness2-job-" + label, hashlib.sha256).digest()


def _openssl(data: bytes, password: bytes, decrypt: bool = False, executable: str | None = None) -> bytes:
    # A private temporary password file works across Windows/POSIX. Its path may
    # appear in argv, but the key never does. The file is unlinked immediately.
    directory = tempfile.gettempdir()
    fd, password_path = tempfile.mkstemp(prefix=".harness2-key-", dir=directory)
    try:
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        except (AttributeError, OSError):
            pass
        os.write(fd, password.hex().encode("ascii") + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    argv = [
        executable or shutil.which("openssl") or "openssl", "enc", "-aes-256-ctr", "-pbkdf2", "-iter", "200000",
        "-salt", "-pass", f"file:{password_path}",
    ]
    if decrypt:
        argv.append("-d")
    try:
        proc = subprocess.run(argv, input=data, capture_output=True, timeout=30)
    finally:
        try:
            os.unlink(password_path)
        except FileNotFoundError:
            pass
    if proc.returncode != 0:
        raise CryptoError((proc.stderr or b"OpenSSL failed").decode("utf-8", "replace")[:300])
    return proc.stdout


def encrypt(master: bytes, plaintext: bytes, executable: str | None = None) -> bytes:
    ciphertext = _openssl(plaintext, _derive(master, b"enc"), executable=executable)
    body = MAGIC + ciphertext
    mac = hmac.new(_derive(master, b"mac"), body, hashlib.sha256).digest()
    return body + mac


def decrypt(master: bytes, envelope: bytes, executable: str | None = None) -> bytes:
    if len(envelope) < len(MAGIC) + 16 + MAC_SIZE or not envelope.startswith(MAGIC):
        raise CryptoError("invalid job envelope")
    body, supplied = envelope[:-MAC_SIZE], envelope[-MAC_SIZE:]
    expected = hmac.new(_derive(master, b"mac"), body, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        raise CryptoError("job envelope authentication failed")
    ciphertext = body[len(MAGIC):]
    return _openssl(ciphertext, _derive(master, b"enc"), decrypt=True, executable=executable)


def atomic_write_envelope(path: str, payload: bytes) -> None:
    parent = ensure_private_dir(os.path.dirname(os.path.abspath(path)))
    fd, tmp = tempfile.mkstemp(prefix=".job.", dir=parent)
    try:
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        except (AttributeError, OSError):
            pass
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        try:
            os.chmod(path, PRIVATE_FILE_MODE)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
