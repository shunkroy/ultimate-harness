"""Private, content-addressed ContextPackage writer and verifier."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Tuple

from ..security import atomic_write_bytes, atomic_write_json, ensure_private_dir, read_private_json
from .compiler import CompiledContext, ContextCompiler
from .schema import CONTEXT_SCHEMA, ContextIR, OperationContract, SemanticUnit, SourceRef


class PackageError(ValueError):
    pass


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unit(raw: Mapping[str, Any]) -> SemanticUnit:
    return SemanticUnit(
        str(raw["id"]), str(raw["kind"]), str(raw["text"]), str(raw["source_id"]),
        int(raw["line_start"]), int(raw["line_end"]), float(raw.get("confidence", 1.0)),
        str(raw.get("uncertainty", "certain")),
    )


def _operation(raw: Mapping[str, Any]) -> OperationContract:
    permissions = tuple(str(item) for item in raw.get("permissions", []))
    if permissions:
        raise PackageError("context package v1 does not authorize runtime permissions")
    return OperationContract(
        str(raw["name"]), str(raw["operation_class"]),
        {str(key): str(value) for key, value in dict(raw["inputs"]).items()},
        str(raw["output"]), str(raw["implementation"]), bool(raw.get("pure", True)), permissions,
    )


def _ir(raw: Mapping[str, Any]) -> ContextIR:
    if raw.get("schema") != "harness.context-ir/v1":
        raise PackageError("unsupported ContextIR schema")
    source_raw = dict(raw["source"])
    source = SourceRef(
        str(source_raw["id"]), str(source_raw["path"]), str(source_raw["sha256"]),
        str(source_raw.get("media_type", "text/plain")),
    )
    permissions = tuple(str(item) for item in raw.get("permissions", []))
    if permissions:
        raise PackageError("context package requests unsupported permissions")
    return ContextIR(
        str(raw["context_id"]), str(raw["name"]), str(raw["version"]), source,
        tuple(_unit(item) for item in raw.get("concepts", [])),
        tuple(_unit(item) for item in raw.get("rules", [])),
        tuple(_unit(item) for item in raw.get("procedures", [])),
        tuple(_unit(item) for item in raw.get("examples", [])),
        tuple(_operation(item) for item in raw.get("operations", [])),
        tuple(dict(item) for item in raw.get("contradictions", [])),
        permissions, dict(raw.get("state_schema", {})),
    )


@dataclass(frozen=True)
class ContextPackage:
    root: str
    manifest: Mapping[str, Any]
    ir: ContextIR
    source_text: str

    @classmethod
    def write(cls, compiled: CompiledContext, destination: str) -> "ContextPackage":
        root = ensure_private_dir(destination)
        sources = ensure_private_dir(os.path.join(root, "sources"))
        source_path = os.path.join(root, compiled.ir.source.path)
        if os.path.dirname(source_path) != sources:
            raise PackageError("invalid source path")
        atomic_write_bytes(source_path, compiled.source_bytes)
        ir_value = compiled.ir.as_dict()
        ir_bytes = _canonical(ir_value)
        atomic_write_bytes(os.path.join(root, "ir.json"), ir_bytes)
        manifest = {
            "schema": CONTEXT_SCHEMA,
            "context_id": compiled.ir.context_id,
            "name": compiled.ir.name,
            "version": compiled.ir.version,
            "compiler": {"name": "ContextCompiler", "version": ContextCompiler.VERSION},
            "compiled_at": datetime.now(timezone.utc).isoformat(),
            "pure": not bool(compiled.ir.state_schema),
            "permissions": [],
            "source": compiled.ir.source.as_dict(),
            "ir": {"path": "ir.json", "sha256": _sha(ir_bytes)},
            "operations": [item.as_dict() for item in compiled.ir.operations],
        }
        atomic_write_json(os.path.join(root, "manifest.json"), manifest)
        return cls.load(root)

    @classmethod
    def load(cls, destination: str) -> "ContextPackage":
        root = os.path.abspath(os.path.expanduser(destination))
        st = os.lstat(root)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise PackageError("context package must be a non-symlink directory")
        manifest_path = os.path.join(root, "manifest.json")
        manifest = read_private_json(manifest_path)
        if manifest.get("schema") != CONTEXT_SCHEMA or manifest.get("permissions") != []:
            raise PackageError("invalid or unauthorized context manifest")
        ir_meta = manifest.get("ir")
        source_meta = manifest.get("source")
        if not isinstance(ir_meta, dict) or not isinstance(source_meta, dict):
            raise PackageError("context manifest is incomplete")
        ir_path = os.path.abspath(os.path.join(root, str(ir_meta.get("path", ""))))
        source_path = os.path.abspath(os.path.join(root, str(source_meta.get("path", ""))))
        if os.path.commonpath((root, ir_path)) != root or os.path.commonpath((root, source_path)) != root:
            raise PackageError("context package path escapes package root")
        for path in (manifest_path, ir_path, source_path):
            file_st = os.lstat(path)
            if stat.S_ISLNK(file_st.st_mode) or not stat.S_ISREG(file_st.st_mode):
                raise PackageError("context package contains a non-regular file")
        with open(ir_path, "rb") as fh:
            ir_bytes = fh.read()
        with open(source_path, "rb") as fh:
            source_bytes = fh.read()
        if not _HEX64.fullmatch(str(ir_meta.get("sha256", ""))) or _sha(ir_bytes) != ir_meta["sha256"]:
            raise PackageError("ContextIR hash mismatch")
        if not _HEX64.fullmatch(str(source_meta.get("sha256", ""))) or _sha(source_bytes) != source_meta["sha256"]:
            raise PackageError("context source hash mismatch")
        try:
            raw_ir = json.loads(ir_bytes.decode("utf-8"))
            source_text = source_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise PackageError("context package contains invalid UTF-8/JSON") from exc
        ir = _ir(raw_ir)
        if ir.context_id != manifest.get("context_id") or ir.version != manifest.get("version"):
            raise PackageError("context identity/version mismatch")
        if tuple(item.as_dict() for item in ir.operations) != tuple(manifest.get("operations", [])):
            raise PackageError("context operation manifest mismatch")
        return cls(root, manifest, ir, source_text)
