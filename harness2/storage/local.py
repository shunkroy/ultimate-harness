"""Private local immutable object storage with contextual authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import time
from typing import Any, Mapping

from ..crypto import decrypt, encrypt, load_or_create_key
from ..kernel.payloads import (
    MAX_PAYLOAD_BYTES,
    PayloadError,
    PayloadIntegrityError,
    PayloadReference,
    canonical_bytes,
    content_identity,
)
from ..security import atomic_write_bytes, ensure_private_dir


_OBJECT = re.compile(r"^[0-9a-f]{64}$")


class LocalAuthenticatedStorage:
    backend_id = "local.authenticated/v1"
    envelope_version = 1

    def __init__(self, root: str, key_path: str, *, openssl_bin: str | None = None):
        self.root = ensure_private_dir(root)
        self.key_path = key_path
        self.master = load_or_create_key(key_path)
        self.openssl_bin = openssl_bin
        self.key_id = hashlib.sha256(b"harness-object-key/v1\0" + self.master).hexdigest()[:32]

    def _derive(self, label: bytes) -> bytes:
        return hmac.new(self.master, b"harness-object/v1\0" + label, hashlib.sha256).digest()

    @staticmethod
    def _binding(value: Mapping[str, Any]) -> bytes:
        return canonical_bytes(dict(value), max_bytes=64 * 1024)

    def _reference_body(
        self, *, reference_id: str, object_key: str, content_hash: str,
        size_bytes: int, media_type: str, schema_id: str, purpose: str,
        binding_hash: str,
    ) -> bytes:
        return canonical_bytes({
            "reference_id": reference_id, "backend_id": self.backend_id,
            "object_key": object_key, "content_sha256": content_hash,
            "size_bytes": size_bytes, "media_type": media_type,
            "schema_id": schema_id, "purpose": purpose,
            "envelope_version": self.envelope_version, "key_id": self.key_id,
            "binding_sha256": binding_hash,
        }, max_bytes=16 * 1024)

    def _path(self, object_key: str) -> str:
        if not _OBJECT.fullmatch(object_key):
            raise PayloadError("invalid opaque object key")
        directory = ensure_private_dir(os.path.join(self.root, object_key[:2]))
        path = os.path.join(directory, object_key[2:] + ".blob")
        if os.path.dirname(path) != directory:
            raise PayloadError("object path escaped storage root")
        return path

    def put(
        self, data: bytes, *, schema_id: str, purpose: str,
        binding: Mapping[str, Any], media_type: str = "application/json",
    ) -> PayloadReference:
        if not isinstance(data, bytes) or len(data) > MAX_PAYLOAD_BYTES:
            raise PayloadError("object data must be bounded bytes")
        binding_bytes = self._binding(binding)
        binding_hash = hashlib.sha256(binding_bytes).hexdigest()
        content_hash = content_identity(schema_id=schema_id, purpose=purpose, plaintext=data)
        reference_id = "ref-" + hashlib.sha256(
            b"harness-reference/v1\0" + content_hash.encode() + b"\0" + binding_hash.encode()
        ).hexdigest()
        object_key = hashlib.sha256(
            b"harness-object-key/v1\0" + reference_id.encode()
        ).hexdigest()
        aad = self._reference_body(
            reference_id=reference_id, object_key=object_key,
            content_hash=content_hash, size_bytes=len(data), media_type=media_type,
            schema_id=schema_id, purpose=purpose, binding_hash=binding_hash,
        )
        encrypted = encrypt(self._derive(b"envelope"), data, self.openssl_bin)
        envelope_mac = hmac.new(self._derive(b"object-mac"), aad + encrypted, hashlib.sha256).digest()
        envelope = b"H2O1" + len(aad).to_bytes(4, "big") + aad + encrypted + envelope_mac
        path = self._path(object_key)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            atomic_write_bytes(path, envelope)
        else:
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise PayloadIntegrityError("object destination is not a regular file")
        reference_mac = hmac.new(self._derive(b"reference-mac"), aad, hashlib.sha256).hexdigest()
        reference = PayloadReference(
            reference_id, self.backend_id, object_key, content_hash, len(data),
            media_type, schema_id, purpose, self.envelope_version,
            self.key_id, reference_mac,
        )
        if os.path.exists(path) and self.get(reference, binding=binding) != data:
            raise PayloadIntegrityError("immutable object identity collision")
        return reference

    def get(self, reference: PayloadReference, *, binding: Mapping[str, Any]) -> bytes:
        if reference.backend_id != self.backend_id or reference.key_id != self.key_id:
            raise PayloadIntegrityError("payload backend/key identity mismatch")
        binding_hash = hashlib.sha256(self._binding(binding)).hexdigest()
        aad = self._reference_body(
            reference_id=reference.reference_id, object_key=reference.object_key,
            content_hash=reference.content_sha256, size_bytes=reference.size_bytes,
            media_type=reference.media_type, schema_id=reference.schema_id,
            purpose=reference.purpose, binding_hash=binding_hash,
        )
        expected_ref = hmac.new(self._derive(b"reference-mac"), aad, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(reference.reference_mac, expected_ref):
            raise PayloadIntegrityError("payload reference authentication failed")
        path = self._path(reference.object_key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise PayloadIntegrityError("referenced object is unavailable") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_PAYLOAD_BYTES + 1024 * 1024:
                raise PayloadIntegrityError("invalid object file")
            chunks: list[bytes] = []
            remaining = st.st_size + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            envelope = b"".join(chunks)
        finally:
            os.close(fd)
        if len(envelope) != st.st_size:
            raise PayloadIntegrityError("object file changed or was read incompletely")
        if len(envelope) < 4 + 4 + 32 or not envelope.startswith(b"H2O1"):
            raise PayloadIntegrityError("invalid object envelope")
        aad_len = int.from_bytes(envelope[4:8], "big")
        if aad_len < 2 or aad_len > 16 * 1024 or len(envelope) < 8 + aad_len + 32:
            raise PayloadIntegrityError("invalid object associated data length")
        stored_aad = envelope[8:8 + aad_len]
        encrypted = envelope[8 + aad_len:-32]
        supplied = envelope[-32:]
        expected = hmac.new(self._derive(b"object-mac"), stored_aad + encrypted, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected) or not hmac.compare_digest(stored_aad, aad):
            raise PayloadIntegrityError("object contextual authentication failed")
        plaintext = decrypt(self._derive(b"envelope"), encrypted, self.openssl_bin)
        if len(plaintext) != reference.size_bytes:
            raise PayloadIntegrityError("object plaintext size mismatch")
        actual = content_identity(
            schema_id=reference.schema_id, purpose=reference.purpose, plaintext=plaintext,
        )
        if not hmac.compare_digest(actual, reference.content_sha256):
            raise PayloadIntegrityError("object content identity mismatch")
        return plaintext

    def verify(self, reference: PayloadReference, *, binding: Mapping[str, Any]) -> bool:
        self.get(reference, binding=binding)
        return True
