"""Deterministic structured-text to ContextIR compiler."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple

from .schema import ContextIR, OperationContract, SemanticUnit, SourceRef


class CompileError(ValueError):
    pass


_SECTIONS = {
    "concepts": "concept",
    "rules": "rule",
    "procedures": "procedure",
    "examples": "example",
    "operations": "operation",
}
_DECLARATION = re.compile(
    r"^([a-zA-Z][a-zA-Z0-9_]*)\s*\(([^)]*)\)\s*->\s*([a-zA-Z][a-zA-Z0-9_\[\]]*)\s*$"
)
_ALLOWED_OPERATIONS = {
    "query": ("query", {"topic": "string"}, "EvidenceSet", "deterministic.search"),
    "transform": ("transform", {"text": "string", "mode": "string"}, "Text", "deterministic.transform"),
    "generate": ("generate", {"topic": "string"}, "EvidenceBrief", "deterministic.evidence_brief"),
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    if not text:
        raise CompileError("context name does not produce a valid identifier")
    return text[:96]


def _inputs(raw: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not raw.strip():
        return values
    for item in raw.split(","):
        parts = [value.strip() for value in item.split(":", 1)]
        if len(parts) != 2 or not parts[0] or parts[1] not in {"string", "integer", "number", "boolean", "object", "array"}:
            raise CompileError(f"invalid operation input declaration: {item.strip()}")
        if parts[0] in values:
            raise CompileError(f"duplicate operation input: {parts[0]}")
        values[parts[0]] = parts[1]
    return values


@dataclass(frozen=True)
class CompiledContext:
    ir: ContextIR
    source_bytes: bytes


class ContextCompiler:
    VERSION = "0.1.0"

    def compile_bytes(
        self, raw: bytes, *, name: str, version: str = "0.1.0",
        source_name: str = "source.txt",
    ) -> CompiledContext:
        if not isinstance(raw, bytes):
            raise CompileError("context source must be bytes")
        if len(raw) > 10 * 1024 * 1024:
            raise CompileError("context source exceeds the v1 10 MiB limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CompileError("context source must be UTF-8 text") from exc
        compiled = self.compile_text(
            text, name=name, version=version, source_name=source_name,
        )
        return CompiledContext(compiled.ir, raw)

    def compile_text(self, text: str, *, name: str, version: str = "0.1.0", source_name: str = "source.txt") -> CompiledContext:
        if not isinstance(text, str) or not text.strip():
            raise CompileError("context source is empty")
        source_bytes = text.encode("utf-8")
        source_hash = _digest(source_bytes)
        context_id = f"ctx-{_slug(name)}-{source_hash[:12]}"
        source_id = f"src-{source_hash[:16]}"
        source = SourceRef(source_id, f"sources/{source_hash}.txt", source_hash)
        sections: Dict[str, list[SemanticUnit]] = {key: [] for key in _SECTIONS if key != "operations"}
        operations: Dict[str, OperationContract] = {}
        section: Optional[str] = None
        unit_index = 0
        for line_number, raw in enumerate(text.splitlines(), 1):
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip().lower()
                section = heading if heading in _SECTIONS else None
                continue
            if section == "operations":
                match = _DECLARATION.fullmatch(stripped)
                if not match:
                    raise CompileError(f"invalid operation declaration at line {line_number}")
                operation_name, raw_inputs, output = match.groups()
                if operation_name not in _ALLOWED_OPERATIONS:
                    raise CompileError(f"operation is not authorized by compiler v1: {operation_name}")
                operation_class, expected_inputs, expected_output, implementation = _ALLOWED_OPERATIONS[operation_name]
                declared_inputs = _inputs(raw_inputs)
                if declared_inputs != expected_inputs or output != expected_output:
                    raise CompileError(f"operation contract mismatch for {operation_name}")
                if operation_name in operations:
                    raise CompileError(f"duplicate operation: {operation_name}")
                operations[operation_name] = OperationContract(
                    operation_name, operation_class, declared_inputs, output,
                    implementation, True, (),
                )
                continue
            target = section if section in sections else "concepts"
            unit_index += 1
            sections[target].append(SemanticUnit(
                f"unit-{unit_index:06d}", _SECTIONS[target], stripped,
                source_id, line_number, line_number,
            ))
        if not operations:
            for operation_name in ("query", "transform"):
                operation_class, expected_inputs, output, implementation = _ALLOWED_OPERATIONS[operation_name]
                operations[operation_name] = OperationContract(
                    operation_name, operation_class, expected_inputs, output,
                    implementation, True, (),
                )
        ir = ContextIR(
            context_id, name.strip(), version, source,
            tuple(sections["concepts"]), tuple(sections["rules"]),
            tuple(sections["procedures"]), tuple(sections["examples"]),
            tuple(operations[name] for name in sorted(operations)),
            permissions=(), state_schema={},
        )
        return CompiledContext(ir, source_bytes)

    def compile_file(self, path: str, *, name: Optional[str] = None, version: str = "0.1.0") -> CompiledContext:
        absolute = os.path.abspath(os.path.expanduser(path))
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(absolute, flags)
        except OSError as exc:
            raise CompileError("context source could not be opened safely") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise CompileError("context source must be a regular non-symlink file")

            def read_once() -> bytes:
                os.lseek(fd, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, min(1024 * 1024, 10 * 1024 * 1024 + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 10 * 1024 * 1024:
                        raise CompileError("context source exceeds the v1 10 MiB limit")
                return b"".join(chunks)

            raw = read_once()
            if read_once() != raw:
                raise CompileError("context source changed while it was read")
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            ):
                raise CompileError("context source changed while it was read")
        finally:
            os.close(fd)
        return self.compile_bytes(
            raw, name=name or Path(absolute).stem, version=version,
            source_name=os.path.basename(absolute),
        )
