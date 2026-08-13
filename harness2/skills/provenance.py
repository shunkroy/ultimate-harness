"""Authenticated producer statements and persistent provenance observations."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping

from ..kernel.event_bus import EventBus, TypedEvent, canonical_json
from ..kernel.execution_state import ExecutionStateRepository
from ..kernel.payloads import PayloadIntegrityError, PayloadReference, canonical_bytes


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ProvenanceError(ValueError):
    pass


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    INVALID = "invalid"
    UNKNOWN_PRODUCER = "unknown_producer"
    EXPIRED = "expired"
    UNSUPPORTED_METHOD = "unsupported_method"
    TEST_ONLY = "test_only"


@dataclass(frozen=True)
class SignedStatement:
    subject_kind: str
    subject_id: str
    subject_sha256: str
    evidence_kind: str
    artifact_sha256: str
    issued_at: float
    expires_at: float | None
    nonce: str
    producer_id: str
    key_id: str
    signature: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ProvenanceError("unsupported provenance statement schema")
        for value, name in (
            (self.subject_kind, "subject_kind"), (self.subject_id, "subject_id"),
            (self.evidence_kind, "evidence_kind"), (self.nonce, "nonce"),
            (self.producer_id, "producer_id"), (self.key_id, "key_id"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ProvenanceError(f"invalid {name}")
        for value in (self.subject_sha256, self.artifact_sha256):
            if not _HEX64.fullmatch(value):
                raise ProvenanceError("provenance hashes must be lowercase SHA-256")
        if not isinstance(self.issued_at, (int, float)) or not math.isfinite(self.issued_at):
            raise ProvenanceError("provenance issued_at must be finite")
        if self.expires_at is not None and (
            not isinstance(self.expires_at, (int, float)) or not math.isfinite(self.expires_at)
        ):
            raise ProvenanceError("provenance expires_at must be finite")
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise ProvenanceError("provenance expiry must follow issuance")
        if self.signature and not _HEX64.fullmatch(self.signature):
            raise ProvenanceError("signature must be lowercase HMAC-SHA256")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subject_kind": self.subject_kind, "subject_id": self.subject_id,
            "subject_sha256": self.subject_sha256, "evidence_kind": self.evidence_kind,
            "artifact_sha256": self.artifact_sha256, "issued_at": self.issued_at,
            "expires_at": self.expires_at, "nonce": self.nonce,
            "producer_id": self.producer_id, "key_id": self.key_id,
        }

    @property
    def signed_bytes(self) -> bytes:
        return b"harness.provenance-statement/v1\0" + canonical_bytes(
            self.unsigned_dict(), max_bytes=64 * 1024,
        )

    @property
    def statement_hash(self) -> str:
        return hashlib.sha256(self.signed_bytes + b"\0" + self.signature.encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}


@dataclass(frozen=True)
class ProvenanceVerification:
    statement: SignedStatement
    status: VerificationStatus
    method: str
    authenticated_producer_id: str
    trust_domain: str
    test_only: bool = False
    reason_code: str = ""


class LocalHMACProducerVerifier:
    """Local integrity authentication, not external or human identity proof."""

    method = "hmac-sha256-local/v1"

    def __init__(
        self, producer_id: str, key_id: str, key: bytes, *,
        trust_domain: str = "local", test_only: bool = False,
    ):
        if not producer_id.strip() or not key_id.strip() or len(key) < 32:
            raise ProvenanceError("invalid local producer verifier")
        self.producer_id = producer_id
        self.key_id = key_id
        self.key = bytes(key)
        self.trust_domain = trust_domain
        self.test_only = test_only

    def sign(self, statement: SignedStatement) -> SignedStatement:
        if statement.producer_id != self.producer_id or statement.key_id != self.key_id:
            raise ProvenanceError("statement producer/key does not match signer")
        signature = hmac.new(self.key, statement.signed_bytes, hashlib.sha256).hexdigest()
        return replace(statement, signature=signature)

    def verify(self, statement: SignedStatement, *, now: float | None = None) -> ProvenanceVerification:
        observed = time.time() if now is None else float(now)
        if statement.producer_id != self.producer_id or statement.key_id != self.key_id:
            status, reason = VerificationStatus.UNKNOWN_PRODUCER, "producer_key_unknown"
        elif statement.issued_at > observed:
            status, reason = VerificationStatus.INVALID, "statement_from_future"
        elif statement.expires_at is not None and statement.expires_at < observed:
            status, reason = VerificationStatus.EXPIRED, "statement_expired"
        else:
            expected = hmac.new(self.key, statement.signed_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(statement.signature, expected):
                status, reason = VerificationStatus.INVALID, "signature_invalid"
            elif self.test_only:
                status, reason = VerificationStatus.TEST_ONLY, "test_only_producer"
            else:
                status, reason = VerificationStatus.VERIFIED, "signature_verified"
        return ProvenanceVerification(
            statement, status, self.method, self.producer_id,
            self.trust_domain, self.test_only, reason,
        )


@dataclass(frozen=True)
class ProvenanceObservation:
    observation_id: str
    subject_kind: str
    subject_id: str
    subject_sha256: str
    evidence_kind: str
    producer_identity: str
    verification_method: str
    verification_status: VerificationStatus
    statement_hash: str
    trust_domain: str
    test_only: bool
    observed_at: float
    expires_at: float | None
    evidence_reference_id: str


class ProvenanceRepository:
    def __init__(
        self, store, events: EventBus, execution_state: ExecutionStateRepository,
        verifiers: tuple[LocalHMACProducerVerifier, ...] = (),
    ):
        self.store = store
        self.events = events
        self.execution_state = execution_state
        self.objects = execution_state.objects
        self.verifiers = {
            (item.producer_id, item.key_id, item.method): item for item in verifiers
        }

    def observe(
        self, statement: SignedStatement, *, now: float | None = None,
        method: str = LocalHMACProducerVerifier.method,
    ) -> ProvenanceObservation:
        observed = time.time() if now is None else float(now)
        verifier = self.verifiers.get((statement.producer_id, statement.key_id, method))
        if verifier is None:
            raise ProvenanceError("producer verifier is not registered")
        verification = verifier.verify(statement, now=observed)
        data = canonical_bytes(statement.as_dict(), max_bytes=64 * 1024)
        binding = {
            "subject_kind": statement.subject_kind,
            "subject_id": statement.subject_id,
            "subject_sha256": statement.subject_sha256,
            "producer_id": verification.authenticated_producer_id,
            "purpose": "provenance.statement",
        }
        reference = self.objects.put(
            data, schema_id="harness.provenance-statement/1",
            purpose="provenance.statement", binding=binding,
        )
        observation_id = "provenance-" + hashlib.sha256(
            b"harness.provenance-observation/v1\0" + statement.statement_hash.encode()
            + b"\0" + verification.status.value.encode()
        ).hexdigest()
        metadata = canonical_json({
            "evidence_kind": statement.evidence_kind,
            "key_id": statement.key_id,
            "statement_hash": statement.statement_hash,
            "trust_domain": verification.trust_domain,
            "test_only": verification.test_only,
            "reason_code": verification.reason_code,
        }, limit=16 * 1024)
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self.execution_state._insert_reference(con, reference, observed)
            event = self.events.append(TypedEvent(
                event_type="provenance.observed", source="skills.provenance",
                correlation_id=observation_id,
                dedup_key=observation_id,
                payload={
                    "subject_kind": statement.subject_kind,
                    "subject_id": statement.subject_id,
                    "subject_sha256": statement.subject_sha256,
                    "evidence_kind": statement.evidence_kind,
                    "producer_identity": verification.authenticated_producer_id,
                    "verification_method": verification.method,
                    "verification_status": verification.status.value,
                    "statement_hash": statement.statement_hash,
                },
            ), connection=con)
            con.execute(
                "INSERT OR IGNORE INTO kernel_provenance_observations("
                "observation_id,subject_kind,subject_id,subject_sha256,producer_identity,"
                "verification_method,verification_status,signature_metadata_json,"
                "evidence_reference_id,observed_at,expires_at,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation_id, statement.subject_kind, statement.subject_id,
                    statement.subject_sha256, verification.authenticated_producer_id,
                    verification.method, verification.status.value, metadata,
                    reference.reference_id, observed, statement.expires_at, event.event_id,
                ),
            )
        return ProvenanceObservation(
            observation_id, statement.subject_kind, statement.subject_id,
            statement.subject_sha256, statement.evidence_kind,
            verification.authenticated_producer_id, verification.method,
            verification.status, statement.statement_hash,
            verification.trust_domain, verification.test_only, observed,
            statement.expires_at, reference.reference_id,
        )

    def latest_verified(
        self, *, subject_kind: str, subject_id: str, subject_sha256: str,
        evidence_kind: str, at: float | None = None,
        accepted_methods: frozenset[str] = frozenset({"hmac-sha256-local/v1"}),
        production: bool = True,
    ) -> ProvenanceObservation | None:
        observed = time.time() if at is None else float(at)
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT * FROM kernel_provenance_observations WHERE subject_kind=? "
                "AND subject_id=? AND subject_sha256=? ORDER BY observed_at DESC",
                (subject_kind, subject_id, subject_sha256),
            ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(str(row["signature_metadata_json"]))
            except (TypeError, ValueError):
                continue
            if metadata.get("evidence_kind") != evidence_kind:
                continue
            stored_status = VerificationStatus(str(row["verification_status"]))
            if stored_status not in {VerificationStatus.VERIFIED, VerificationStatus.TEST_ONLY}:
                continue
            if str(row["verification_method"]) not in accepted_methods:
                continue
            if row["expires_at"] is not None and float(row["expires_at"]) < observed:
                continue
            reference = self._evidence_reference(str(row["evidence_reference_id"]))
            producer = str(row["producer_identity"])
            binding = {
                "subject_kind": subject_kind, "subject_id": subject_id,
                "subject_sha256": subject_sha256, "producer_id": producer,
                "purpose": "provenance.statement",
            }
            data = self.objects.get(reference, binding=binding)
            statement_hash = str(metadata.get("statement_hash", ""))
            raw = json.loads(data.decode("utf-8"))
            statement = SignedStatement(**raw)
            if statement.statement_hash != statement_hash:
                raise PayloadIntegrityError("provenance statement identity mismatch")
            verifier = self.verifiers.get((
                statement.producer_id, statement.key_id, str(row["verification_method"]),
            ))
            if verifier is None:
                continue
            current = verifier.verify(statement, now=observed)
            if current.status not in {VerificationStatus.VERIFIED, VerificationStatus.TEST_ONLY}:
                continue
            if current.status != stored_status:
                raise PayloadIntegrityError("provenance verification status mismatch")
            if production and (current.test_only or current.status == VerificationStatus.TEST_ONLY):
                continue
            return ProvenanceObservation(
                str(row["observation_id"]), subject_kind, subject_id, subject_sha256,
                evidence_kind, producer, str(row["verification_method"]), current.status,
                statement_hash, str(metadata.get("trust_domain", "")),
                bool(metadata.get("test_only")), float(row["observed_at"]),
                float(row["expires_at"]) if row["expires_at"] is not None else None,
                reference.reference_id,
            )
        return None

    def _evidence_reference(self, reference_id: str) -> PayloadReference:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM kernel_payload_references WHERE reference_id=?", (reference_id,),
            ).fetchone()
        if not row:
            raise PayloadIntegrityError("provenance evidence reference is missing")
        return self.execution_state._reference_from_row(row)

    def verified_skill_evidence(
        self, evidence, *, skill_id: str, version: str,
        production: bool = True, at: float | None = None,
    ):
        from .contracts import VerifiedSkillEvidence
        observation = self.latest_verified(
            subject_kind="skill_manifest", subject_id=f"{skill_id}@{version}",
            subject_sha256=evidence.subject_hash, evidence_kind=evidence.kind,
            at=at, production=production,
        )
        if observation is None:
            raise ProvenanceError("verified provenance is unavailable for skill evidence")
        reference = self._evidence_reference(observation.evidence_reference_id)
        binding = {
            "subject_kind": observation.subject_kind,
            "subject_id": observation.subject_id,
            "subject_sha256": observation.subject_sha256,
            "producer_id": observation.producer_identity,
            "purpose": "provenance.statement",
        }
        raw = json.loads(self.objects.get(reference, binding=binding).decode("utf-8"))
        statement = SignedStatement(**raw)
        if statement.artifact_sha256 != evidence.artifact_hash:
            raise ProvenanceError("provenance artifact does not match evidence")
        result = VerifiedSkillEvidence(
            evidence, observation.verification_status.value,
            observation.verification_method, observation.producer_identity,
            observation.trust_domain, observation.statement_hash,
            statement.artifact_sha256, statement.evidence_kind,
            observation.expires_at, observation.test_only,
        )
        return result

    def validate_skill_evidence(
        self, verified, *, skill_id: str, version: str,
        production: bool = True, at: float | None = None,
    ) -> bool:
        try:
            current = self.verified_skill_evidence(
                verified.evidence, skill_id=skill_id, version=version,
                production=production, at=at,
            )
        except (ProvenanceError, PayloadIntegrityError, ValueError):
            return False
        return current == verified
