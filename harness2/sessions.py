"""Persistent Harness-owned conversation sessions (Phase 10 first slice).

A session belongs to Harness, never to a provider: turn history is durable,
provider-fluid (each turn records the engine/provider/model actually used) and
stored with authenticated encryption (domain-separated session envelopes, see
``crypto.encrypt_session_turn``). Provider switching therefore continues one
logical conversation because history is rebuilt from the session store, not
from any model's memory.

Turn lifecycle::

    user turn appended (status=pending)
        -> processing (run in flight)
        -> completed (+ assistant turn appended)   normal outcome
        -> interrupted                             crash/failure before a result

A crash between user-turn persistence and the assistant result leaves the
user turn in an unfinished state; it is never silently turned into a
successful exchange and never duplicated on explicit retry (the retry reuses
the trailing unfinished turn when the prompt matches).

Session history is a governance input, not authority: content is injected
with explicit framing, bounded by turn/byte limits, and the effective
sensitive/untrusted classification is the OR over the current request and all
included history — never a downgrade.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .crypto import (
    CryptoError,
    decrypt_session_turn,
    encrypt_session_turn,
    load_or_create_key,
)
from .models import EngineResult, RoutingDecision, RunRequest
from .security import redact

SESSION_ENVELOPE_VERSION = 1
DEFAULT_KEY_FILE = "job.key"

#: Turn statuses that mean "no assistant result yet" (crash-safe window).
_UNFINISHED_USER_STATUSES = ("pending", "processing", "interrupted")

_CURRENT_REQUEST_MARKER = "[harness:current-request]"


class SessionError(RuntimeError):
    pass


class SessionClosedError(SessionError):
    pass


@dataclass(frozen=True)
class TurnRecord:
    seq: int
    session_id: str
    role: str
    status: str
    text: str
    engine: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    provider_session_id: Optional[str] = None
    run_id: Optional[str] = None
    sensitive: bool = False
    untrusted: bool = False
    error_code: Optional[str] = None
    duration_ms: Optional[float] = None
    created_at: float = 0.0
    summary_from_seq: Optional[int] = None
    summary_to_seq: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq, "session_id": self.session_id, "role": self.role,
            "status": self.status, "text": self.text, "engine": self.engine,
            "provider": self.provider, "model": self.model,
            "provider_session_id": self.provider_session_id, "run_id": self.run_id,
            "sensitive": self.sensitive, "untrusted": self.untrusted,
            "error_code": self.error_code, "duration_ms": self.duration_ms,
            "created_at": self.created_at, "summary_from_seq": self.summary_from_seq,
            "summary_to_seq": self.summary_to_seq,
        }


@dataclass(frozen=True)
class SessionContext:
    """Bounded, deterministic reconstruction of eligible session history."""

    text: str
    included_seq: Tuple[int, ...]
    effective_sensitive: bool
    effective_untrusted: bool
    truncated: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text, "included_seq": list(self.included_seq),
            "effective_sensitive": self.effective_sensitive,
            "effective_untrusted": self.effective_untrusted,
            "truncated": self.truncated,
        }


class SessionContextBuilder:
    """Deterministic history reconstruction for provider-bound context.

    Eligibility: completed user/assistant turns plus completed summaries.
    Tool/error records are never injected as authority. Each turn is framed as
    one canonical JSON line (``{"role": ..., "seq": ..., "content": ...}``) so
    delimiter collisions inside content cannot corrupt framing.

    Selection prefers the **most recent** eligible turns: the newest turns
    that fit within both budgets are chosen, then emitted in chronological
    order, so recent references survive long conversations. Both limits are
    strict: a turn that cannot fit the remaining byte budget is skipped,
    never injected, so the encoded turn payload never exceeds ``byte_limit``.
    The current user message is never part of the reconstruction and must be
    appended separately by the caller.
    """

    def __init__(self, turn_limit: int = 20, byte_limit: int = 16384):
        if turn_limit < 1:
            raise SessionError("turn_limit must be >= 1")
        if byte_limit < 256:
            raise SessionError("byte_limit must be >= 256")
        self.turn_limit = turn_limit
        self.byte_limit = byte_limit

    def _eligible(self, turn: TurnRecord) -> bool:
        return turn.role in ("user", "assistant", "summary") and turn.status == "completed"

    @staticmethod
    def _line(turn: TurnRecord) -> str:
        return json.dumps(
            {"role": turn.role, "seq": turn.seq, "content": turn.text},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    def build(
        self, turns: Sequence[TurnRecord],
        current_sensitive: bool = False, current_untrusted: bool = False,
    ) -> SessionContext:
        eligible = [turn for turn in turns if self._eligible(turn)]
        selected_reversed: List[TurnRecord] = []
        budget = self.byte_limit
        truncated = False
        for turn in reversed(eligible):
            if len(selected_reversed) >= self.turn_limit:
                truncated = True
                break
            cost = len(self._line(turn).encode("utf-8")) + 1
            if cost > budget:
                truncated = True
                continue
            selected_reversed.append(turn)
            budget -= cost
        selected = list(reversed(selected_reversed))
        body = "\n".join(self._line(turn) for turn in selected)
        text = (
            "<harness-session-context>\n" + body + "\n</harness-session-context>"
            if selected else ""
        )
        return SessionContext(
            text=text,
            included_seq=tuple(turn.seq for turn in selected),
            effective_sensitive=current_sensitive or any(turn.sensitive for turn in selected),
            effective_untrusted=current_untrusted or any(turn.untrusted for turn in selected),
            truncated=truncated,
        )


class SessionService:
    """Durable, encrypted, provider-fluid conversation sessions."""

    def __init__(
        self, store, key_path: Optional[str] = None, openssl_bin: Optional[str] = None,
    ) -> None:
        self.store = store
        if key_path is None:
            key_path = os.path.join(os.path.dirname(os.path.abspath(store.path)), DEFAULT_KEY_FILE)
        self.key_path = os.path.abspath(key_path)
        self._key = load_or_create_key(self.key_path)
        self.openssl_bin = openssl_bin
        self.builder = SessionContextBuilder()

    # -- envelope helpers -------------------------------------------------
    def _seal(self, text: str) -> bytes:
        return encrypt_session_turn(self._key, text.encode("utf-8"), executable=self.openssl_bin)

    def _open(self, envelope: bytes) -> str:
        try:
            return decrypt_session_turn(self._key, envelope, executable=self.openssl_bin).decode("utf-8")
        except CryptoError as exc:
            raise SessionError(f"cannot decrypt session turn: {exc}") from exc

    # -- lifecycle --------------------------------------------------------
    def create(self, title: str = "", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        session_id = uuid.uuid4().hex
        now = time.time()
        safe = {str(key): redact(value, 300) for key, value in (metadata or {}).items()}
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO sessions(id,title,state,created_at,updated_at,metadata_json) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, title[:200], "open", now, now, json.dumps(safe, sort_keys=True)),
            )
        self.store.append_audit("session.created", session_id, {"title": title[:200]})
        return {"id": session_id, "title": title, "state": "open", "created_at": now}

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT s.id,s.title,s.state,s.created_at,s.updated_at,"
                "(SELECT COUNT(*) FROM session_turns t WHERE t.session_id=s.id) AS turns "
                "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _session_row(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT s.*,(SELECT COUNT(*) FROM session_turns t WHERE t.session_id=s.id) AS turns "
                "FROM sessions s WHERE s.id=?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def require_open(self, session_id: str) -> Dict[str, Any]:
        session = self._session_row(session_id)
        if session is None:
            raise SessionError(f"session not found: {session_id}")
        if session["state"] != "open":
            raise SessionClosedError(f"session is closed: {session_id}")
        return session

    def resume(self, session_id: str) -> Dict[str, Any]:
        session = self._session_row(session_id)
        if session is None:
            raise SessionError(f"session not found: {session_id}")
        if session["state"] != "open":
            now = time.time()
            with self.store.connect() as con:
                con.execute(
                    "UPDATE sessions SET state='open',updated_at=? WHERE id=?",
                    (now, session_id),
                )
            self.store.append_audit("session.resumed", session_id, {})
            session = self._session_row(session_id)
        return session

    def close(self, session_id: str) -> Dict[str, Any]:
        if self._session_row(session_id) is None:
            raise SessionError(f"session not found: {session_id}")
        now = time.time()
        with self.store.connect() as con:
            con.execute(
                "UPDATE sessions SET state='closed',updated_at=? WHERE id=?",
                (now, session_id),
            )
        self.store.append_audit("session.closed", session_id, {})
        return self._session_row(session_id)

    # -- turns ------------------------------------------------------------
    def _turn_row(self, row) -> TurnRecord:
        return TurnRecord(
            seq=int(row["seq"]), session_id=str(row["session_id"]), role=str(row["role"]),
            status=str(row["status"]), text="",
            engine=row["engine"], provider=row["provider"], model=row["model"],
            provider_session_id=row["provider_session_id"], run_id=row["run_id"],
            sensitive=bool(row["sensitive"]), untrusted=bool(row["untrusted"]),
            error_code=row["error_code"], duration_ms=row["duration_ms"],
            created_at=float(row["created_at"]),
            summary_from_seq=row["summary_from_seq"], summary_to_seq=row["summary_to_seq"],
        )

    def append_user_turn(
        self, session_id: str, text: str, *, sensitive: bool = False, untrusted: bool = False,
    ) -> TurnRecord:
        self.require_open(session_id)
        now = time.time()
        envelope = self._seal(text)
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO session_turns(session_id,role,content_envelope,envelope_version,key_id,"
                "status,sensitive,untrusted,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    session_id, "user", envelope, SESSION_ENVELOPE_VERSION,
                    os.path.basename(self.key_path), "pending",
                    1 if sensitive else 0, 1 if untrusted else 0, now,
                ),
            )
            seq = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
            con.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        self.store.append_audit(
            "session.turn.pending", f"{session_id}:{seq}",
            {"role": "user", "sensitive": str(int(sensitive)), "untrusted": str(int(untrusted))},
        )
        return self.turn(session_id, seq)

    def trailing_unfinished_user_turn(self, session_id: str) -> Optional[TurnRecord]:
        """Most recent user turn that has no assistant result yet."""
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM session_turns WHERE session_id=? AND role='user' "
                "AND status IN ('pending','processing','interrupted') "
                "ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        turn = self._turn_row(row)
        turn = TurnRecord(
            seq=turn.seq, session_id=turn.session_id, role=turn.role, status=turn.status,
            text=self._open(row["content_envelope"]),
            engine=turn.engine, provider=turn.provider, model=turn.model,
            provider_session_id=turn.provider_session_id, run_id=turn.run_id,
            sensitive=turn.sensitive, untrusted=turn.untrusted,
            error_code=turn.error_code, duration_ms=turn.duration_ms,
            created_at=turn.created_at,
            summary_from_seq=turn.summary_from_seq, summary_to_seq=turn.summary_to_seq,
        )
        return turn

    def mark_processing(self, session_id: str, seq: int) -> None:
        with self.store.connect() as con:
            con.execute(
                "UPDATE session_turns SET status='processing' WHERE session_id=? AND seq=? AND role='user'",
                (session_id, seq),
            )

    def mark_interrupted(self, session_id: str, seq: int, error_code: str = "interrupted") -> None:
        """Crash/failure before an assistant result: never fabricate a result."""
        with self.store.connect() as con:
            con.execute(
                "UPDATE session_turns SET status='interrupted',error_code=? "
                "WHERE session_id=? AND seq=? AND role='user'",
                (error_code, session_id, seq),
            )

    def append_assistant_turn(
        self, session_id: str, user_seq: int, *, text: str,
        engine: Optional[str], provider: Optional[str], model: Optional[str],
        provider_session_id: Optional[str], run_id: Optional[str],
        sensitive: bool, untrusted: bool, success: bool,
        error_code: Optional[str] = None, duration_ms: Optional[float] = None,
    ) -> TurnRecord:
        now = time.time()
        status = "completed" if success else "failed"
        envelope = self._seal(text or "")
        with self.store.connect() as con:
            con.execute(
                "UPDATE session_turns SET status='completed' WHERE session_id=? AND seq=? AND role='user'",
                (session_id, user_seq),
            )
            con.execute(
                "INSERT INTO session_turns(session_id,role,content_envelope,envelope_version,key_id,"
                "status,engine,provider,model,provider_session_id,run_id,sensitive,untrusted,"
                "error_code,duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, "assistant", envelope, SESSION_ENVELOPE_VERSION,
                    os.path.basename(self.key_path), status,
                    engine, provider, model, provider_session_id, run_id,
                    1 if sensitive else 0, 1 if untrusted else 0,
                    error_code, duration_ms, now,
                ),
            )
            seq = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
            con.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        self.store.append_audit(
            "session.turn.completed", f"{session_id}:{user_seq}",
            {
                "assistant_seq": str(seq), "engine": engine or "",
                "provider": provider or "", "model": model or "",
                "status": status, "error_code": error_code or "",
            },
        )
        return self.turn(session_id, seq)

    def turns(self, session_id: str, limit: int = 100, include_text: bool = True) -> List[TurnRecord]:
        if self._session_row(session_id) is None:
            raise SessionError(f"session not found: {session_id}")
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT * FROM session_turns WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                (session_id, max(1, limit)),
            ).fetchall()
        rows = list(reversed(rows))
        result: List[TurnRecord] = []
        for row in rows:
            turn = self._turn_row(row)
            text = self._open(row["content_envelope"]) if include_text else ""
            result.append(TurnRecord(
                seq=turn.seq, session_id=turn.session_id, role=turn.role, status=turn.status,
                text=text, engine=turn.engine, provider=turn.provider, model=turn.model,
                provider_session_id=turn.provider_session_id, run_id=turn.run_id,
                sensitive=turn.sensitive, untrusted=turn.untrusted,
                error_code=turn.error_code, duration_ms=turn.duration_ms,
                created_at=turn.created_at,
                summary_from_seq=turn.summary_from_seq, summary_to_seq=turn.summary_to_seq,
            ))
        return result

    def turn(self, session_id: str, seq: int) -> TurnRecord:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM session_turns WHERE session_id=? AND seq=?", (session_id, seq),
            ).fetchone()
        if row is None:
            raise SessionError(f"turn not found: {session_id}:{seq}")
        turn = self._turn_row(row)
        return TurnRecord(
            seq=turn.seq, session_id=turn.session_id, role=turn.role, status=turn.status,
            text=self._open(row["content_envelope"]),
            engine=turn.engine, provider=turn.provider, model=turn.model,
            provider_session_id=turn.provider_session_id, run_id=turn.run_id,
            sensitive=turn.sensitive, untrusted=turn.untrusted,
            error_code=turn.error_code, duration_ms=turn.duration_ms,
            created_at=turn.created_at,
            summary_from_seq=turn.summary_from_seq, summary_to_seq=turn.summary_to_seq,
        )

    def info(self, session_id: str, limit: int = 50, include_text: bool = False) -> Dict[str, Any]:
        session = self._session_row(session_id)
        if session is None:
            raise SessionError(f"session not found: {session_id}")
        with self.store.connect() as con:
            attachments = [
                str(row[0]) for row in con.execute(
                    "SELECT context_id FROM session_attachments WHERE session_id=? ORDER BY attached_at",
                    (session_id,),
                ).fetchall()
            ]
        return {
            "id": session["id"], "title": session["title"], "state": session["state"],
            "created_at": session["created_at"], "updated_at": session["updated_at"],
            "metadata": json.loads(session["metadata_json"] or "{}"),
            "attachments": attachments, "turns": session["turns"],
            "history": [turn.as_dict() for turn in self.turns(session_id, limit=limit, include_text=include_text)],
        }

    # -- context reconstruction ------------------------------------------
    def build_context(
        self, session_id: str, current_sensitive: bool = False, current_untrusted: bool = False,
    ) -> SessionContext:
        turns = self.turns(session_id, limit=100000, include_text=True)
        return self.builder.build(turns, current_sensitive, current_untrusted)

    # -- attachment seam (Context-as-Program, future) --------------------
    def attach(self, session_id: str, context_id: str) -> None:
        self.require_open(session_id)
        with self.store.connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO session_attachments(session_id,context_id,attached_at) VALUES(?,?,?)",
                (session_id, context_id, time.time()),
            )


def _final_provider(result: EngineResult, decision: RoutingDecision) -> Optional[str]:
    """Actual final route after fallback; NULL when unknown, never fabricated."""
    meta = result.metadata or {}
    if meta.get("provider"):
        return str(meta["provider"])
    final_route = meta.get("final_route")
    if isinstance(final_route, dict) and final_route.get("provider"):
        return str(final_route["provider"])
    return None


def _final_model(result: EngineResult, decision: RoutingDecision) -> Optional[str]:
    meta = result.metadata or {}
    if meta.get("resolved_model"):
        return str(meta["resolved_model"])
    final_route = meta.get("final_route")
    if isinstance(final_route, dict) and final_route.get("model"):
        return str(final_route["model"])
    return decision.model or None


def run_session_turn(
    service: SessionService, foreground, session_id: str, prompt: str,
    *, sensitive: bool = False, untrusted: bool = False,
    engine: str = "auto", agent: Optional[str] = None, model: Optional[str] = None,
    provider: Optional[str] = None, timeout: int = 240, cwd: Optional[str] = None,
    no_fallback: bool = False, dry_run: bool = False, retries: int = 1,
) -> Dict[str, Any]:
    """Persist a user turn, route it through the existing orchestrator, and
    record the actual final route with the result. Idempotent retry: if the
    trailing user turn is unfinished and matches ``prompt``, it is reused."""
    service.require_open(session_id)
    context = service.build_context(session_id, sensitive, untrusted)
    effective_sensitive = context.effective_sensitive
    effective_untrusted = context.effective_untrusted

    unfinished = service.trailing_unfinished_user_turn(session_id)
    if unfinished is not None and unfinished.text == prompt:
        user_turn = unfinished
        service.mark_processing(session_id, user_turn.seq)
    else:
        user_turn = service.append_user_turn(
            session_id, prompt, sensitive=sensitive, untrusted=untrusted,
        )
        service.mark_processing(session_id, user_turn.seq)

    routed_prompt = prompt
    if context.text:
        routed_prompt = f"{context.text}\n\n{_CURRENT_REQUEST_MARKER}\n{prompt}"

    request = RunRequest(
        prompt=routed_prompt, engine=engine, agent=agent, model=model, provider=provider,
        timeout=timeout, cwd=cwd, sensitive=effective_sensitive, untrusted=effective_untrusted,
        no_fallback=no_fallback, dry_run=dry_run, retries=retries,
        harness_session_id=session_id,
    )
    try:
        decision, result, run_id = foreground.run(request)
    except Exception:
        service.mark_interrupted(session_id, user_turn.seq, "interrupted")
        raise
    assistant = service.append_assistant_turn(
        session_id, user_turn.seq,
        text=result.text or "", engine=result.engine,
        provider=_final_provider(result, decision), model=_final_model(result, decision),
        provider_session_id=result.session_id, run_id=run_id or None,
        sensitive=effective_sensitive, untrusted=effective_untrusted,
        success=result.success, error_code=result.error_code,
        duration_ms=result.duration * 1000.0 if result.duration else None,
    )
    return {
        "session_id": session_id,
        "context": context.as_dict(),
        "user_turn": user_turn.as_dict(),
        "assistant_turn": assistant.as_dict(),
        "decision": {
            "engine": decision.engine, "agent": decision.agent,
            "model": decision.model, "reason": decision.reason,
        },
        "run_id": run_id,
        "success": result.success,
    }