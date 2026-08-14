"""Stdlib-only JSONL event parsers for agent harnesses.

Parses two wire formats into a single typed :class:`ParseResult`:

* ``opencode`` -- OpenCode CLI ``--format json`` event stream. Verified against
  real CLI output (v0.x, Aug 2026). Lines look like::

      {"type":"step_start","sessionID":"ses_...","part":{"type":"step-start",...}}
      {"type":"text","sessionID":"ses_...","part":{"type":"text","text":"..."}}
      {"type":"step_finish","sessionID":"ses_...","part":{"type":"step-finish","reason":"stop",...}}
      {"type":"error","sessionID":"ses_...","error":{"name":"UnknownError","data":{"message":"..."}}}

  Text lives in the nested ``part.text`` of a top-level ``type == "text"`` event.
  An ``error`` event terminates the stream and carries ``error.name`` /
  ``error.data.message``. There is no separate "done" event; the last event of a
  successful run is ``step_finish``.

* ``prime`` -- Prime Agent (pi) ``--mode json`` event stream
  (https://github.com/PrimeIntellect-ai/prime-agent). Lines look like::

      {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"..."}],"stopReason":"stop"}}
      {"type":"turn_end","message":{...},"toolResults":[]}
      {"type":"agent_end","messages":[...]}

  For ``message_end`` / ``turn_end`` the authoritative content, ``errorMessage``
  and ``stopReason`` are nested in ``ev.message``; ``agent_end`` is the terminal
  event and may carry a ``messages`` array. Only assistant (or role-less)
  messages contribute text. ``stopReason`` values ``"error"`` and ``"aborted"``,
  or a non-empty ``errorMessage`` / nested ``error`` object, mark failure.

Design notes:

* Standard library only (``json`` + ``dataclasses``). No regex, no external deps.
* JSON-decodes every line independently; malformed lines are counted and skipped
  without aborting the parse.
* Unicode is preserved byte-for-byte -- text parts are concatenated verbatim,
  never normalized or re-encoded.
* Multipart text is accumulated in stream order across all matching events.
* An error event is always recorded even when partial text was already collected
  (the partial text is kept; ``success`` reflects the error).
"""

from __future__ import annotations

import json
import io
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

__all__ = [
    "ParseResult",
    "parse",
    "parse_opencode",
    "parse_prime",
    "parse_text",
]

JsonObject = Dict[str, Any]

# --- OpenCode ---------------------------------------------------------------

#: Event types that signal the stream ended for OpenCode. A successful run ends
#: with ``step_finish``; a failed run ends with one or more ``error`` events.
OPENCODE_TERMINAL_EVENTS = frozenset({"step_finish", "error"})
OPENCODE_TEXT_EVENT = "text"
OPENCODE_ERROR_EVENT = "error"

# --- Prime ------------------------------------------------------------------

#: Event types whose ``message`` field may carry authoritative content.
PRIME_TEXT_EVENTS = frozenset({"message_end", "turn_end", "agent_end"})
#: The only event that guarantees the whole run is over.
PRIME_TERMINAL_EVENTS = frozenset({"agent_end"})
#: stopReason values that mean the assistant turn failed.
PRIME_ERROR_STOP_REASONS = frozenset({"error", "aborted"})
#: Text blocks with these types are treated as assistant text (``None`` means a
#: plain-string block or a block without a ``type`` field). Everything else
#: (thinking/reasoning, tool_use, image, ...) is excluded.
PRIME_TEXT_BLOCK_TYPES = (None, "text", "input_text")


@dataclass
class ParseResult:
    """Outcome of parsing one JSONL event stream.

    Attributes:
        source: Format name that produced this result (``"opencode"`` or
            ``"prime"``); ``""`` when constructed directly.
        text: All assistant text collected from the stream, in order.
        error: Human-readable error message, or ``None`` when no error seen.
        error_code: Stable error classifier (e.g. OpenCode ``error.name`` or
            Prime ``stopReason``/``errorCode``), or ``None``.
        session_id: Session identifier seen in the stream, or ``None``.
        raw_event_count: Number of raw lines fed to the parser (including
            malformed and blank ones).
        saw_terminal: Whether the stream reached its end marker
            (OpenCode ``step_finish``/``error``, Prime ``agent_end``).
        malformed_count: Number of lines that failed JSON decoding and were
            skipped.
    """

    source: str = ""
    text: str = ""
    error: Optional[str] = None
    error_code: Optional[str] = None
    session_id: Optional[str] = None
    raw_event_count: int = 0
    saw_terminal: bool = False
    malformed_count: int = 0
    terminal_count: int = 0
    trailing_count: int = 0

    @property
    def success(self) -> bool:
        """True only when the stream terminated cleanly with no error.

        A truncated stream (no terminal event) is *not* a success, and neither
        is a stream that ended with an error -- even if partial text exists.
        """
        return not self.error and self.saw_terminal


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _first_str(obj: Any, *keys: str) -> Optional[str]:
    """Return the first string value for ``keys`` in ``obj`` (or ``None``)."""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return None


# ---------------------------------------------------------------------------
# Provider-fluid failure taxonomy
# ---------------------------------------------------------------------------
#
# Engines/providers report failures in their own words. Harness normalizes
# them into stable internal categories so routing, cooldowns and reports can
# reason about *classes* of failure without knowing every vendor's error
# format. The raw vendor error/code always remains attached as evidence.

FAILURE_QUOTA_EXHAUSTED = "quota_exhausted"
FAILURE_RATE_LIMITED = "rate_limited"
FAILURE_AUTHENTICATION = "authentication_failed"
FAILURE_AUTHORIZATION = "authorization_failed"
FAILURE_MODEL_UNAVAILABLE = "model_unavailable"
FAILURE_PROVIDER_UNAVAILABLE = "provider_unavailable"
FAILURE_ENGINE_UNAVAILABLE = "engine_unavailable"
FAILURE_TIMEOUT = "timeout"
FAILURE_NETWORK = "network_failure"
FAILURE_CONTEXT_TOO_LARGE = "context_too_large"
FAILURE_INVALID_REQUEST = "invalid_request"
FAILURE_POLICY_DENIED = "policy_denied"
FAILURE_ENGINE = "engine_failure"
FAILURE_UNKNOWN = "unknown_provider_failure"

#: Ordered (category, needles) pairs; first match wins. Keep rate-limit
#: patterns ahead of generic context patterns ("tokens per minute" is a
#: rate signal, not a context-length signal).
_FAILURE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (FAILURE_RATE_LIMITED, ("rate limit", "too many requests", "429", "tokens per minute", "requests per minute", "tpm", "rpm", "slow down")),
    (FAILURE_QUOTA_EXHAUSTED, ("usage limit", "quota", "insufficient balance", "billing", "out of credits", "credit balance", "plan and billing")),
    (FAILURE_AUTHENTICATION, ("invalid api key", "incorrect api key", "api key is not configured", "missing api key", "authentication failed", "invalid credentials", "401")),
    (FAILURE_AUTHORIZATION, ("forbidden", "permission denied", "not authorized", "authorization failed", "403", "not permitted")),
    (FAILURE_MODEL_UNAVAILABLE, ("model not found", "model does not exist", "unknown model", "model unavailable", "no such model", "model id")),
    (FAILURE_CONTEXT_TOO_LARGE, ("context length", "context window", "too long", "maximum context", "reduce your message size", "token limit", "max tokens")),
    (FAILURE_NETWORK, ("network", "connection", "dns", "socket", "tls", "certificate", "timed out connecting")),
    (FAILURE_POLICY_DENIED, ("policy", "not allowed", "blocked", "refused by policy")),
]

#: Legacy engine-level codes mapped onto the taxonomy.
_FAILURE_CODE_MAP: dict[str, str] = {
    "timeout": FAILURE_TIMEOUT,
    "unavailable": FAILURE_ENGINE_UNAVAILABLE,
    "daemon_unavailable": FAILURE_PROVIDER_UNAVAILABLE,
    "spawn_error": FAILURE_ENGINE,
    "process_error": FAILURE_ENGINE,
    "engine_failure": FAILURE_ENGINE,
    "policy_denied": FAILURE_POLICY_DENIED,
    "disabled": FAILURE_POLICY_DENIED,
    "unsafe_endpoint": FAILURE_POLICY_DENIED,
    "invalid_execution": FAILURE_INVALID_REQUEST,
    "invalid_request": FAILURE_INVALID_REQUEST,
    "invalid_model": FAILURE_MODEL_UNAVAILABLE,
    "missing_credential": FAILURE_AUTHENTICATION,
    "authentication_failed": FAILURE_AUTHENTICATION,
    "context_too_large": FAILURE_CONTEXT_TOO_LARGE,
    "local_error": FAILURE_PROVIDER_UNAVAILABLE,
    "output_limit": FAILURE_CONTEXT_TOO_LARGE,
    "event_limit": FAILURE_CONTEXT_TOO_LARGE,
    "malformed_stream": FAILURE_ENGINE,
    "truncated_stream": FAILURE_ENGINE,
    "trailing_stream": FAILURE_ENGINE,
}


def normalize_failure(error_code: Optional[str], message: Optional[str]) -> str:
    """Map an engine/provider failure into the stable failure taxonomy.

    The raw ``error_code``/``message`` stay attached to the result as
    evidence; this returns the normalized class only.
    """
    if error_code == "circuit_open":
        return FAILURE_PROVIDER_UNAVAILABLE
    if error_code in _FAILURE_CODE_MAP and not (message or "").strip():
        return _FAILURE_CODE_MAP[error_code]
    text = " ".join(part for part in (error_code or "", message or "") if part).lower()
    if not text:
        return FAILURE_UNKNOWN
    for category, needles in _FAILURE_PATTERNS:
        if any(needle in text for needle in needles):
            return category
    if error_code in _FAILURE_CODE_MAP:
        return _FAILURE_CODE_MAP[error_code]
    return FAILURE_UNKNOWN


def _count_and_decode(lines: Iterable[str]) -> tuple[int, int, List[JsonObject]]:
    """Split an iterable of raw lines into (raw_count, malformed_count, events).

    Blank lines count toward ``raw_count`` but produce no event. Non-dict JSON
    values (arrays, scalars) are tolerated and dropped.
    """
    raw_count = 0
    malformed = 0
    events: List[JsonObject] = []
    for raw in lines:
        raw_count += 1
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if isinstance(event, dict):
            events.append(event)
    return raw_count, malformed, events


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------


def _record_opencode_error(result: ParseResult, err: Any) -> None:
    """Extract message + code from an OpenCode ``error`` payload.

    Real shape: ``{"name":"UnknownError","data":{"message":"...","ref":"..."}}``.
    Real streams can emit several error events; the *last* one is kept because
    OpenCode emits a generic error first and the specific root cause last.
    """
    if isinstance(err, str):
        result.error = err
        result.error_code = "error"
        return
    if not isinstance(err, dict):
        result.error = "unknown error"
        result.error_code = "unknown"
        return
    data = err.get("data")
    if isinstance(data, dict):
        message = _first_str(data, "message", "error", "detail")
        code = _first_str(data, "code", "name", "errorCode")
    else:
        message = code = None
    if message is None:
        message = _first_str(err, "message", "error", "detail")
    if code is None:
        code = _first_str(err, "code", "name", "errorCode")
    result.error = message or "unknown error"
    result.error_code = code or "unknown"


def _append_opencode_text(result: ParseResult, event: JsonObject) -> None:
    """Append the text of an OpenCode ``type == "text"`` event.

    Schema: top-level ``part`` object with ``part.text`` (a string). Parts with
    a non-text ``type`` (reasoning, tool, ...) are ignored.
    """
    part = event.get("part")
    if not isinstance(part, dict):
        return
    if part.get("type") not in (None, "text"):
        return
    text = part.get("text")
    if isinstance(text, str):
        result.text += text


def _strict_result(result: ParseResult) -> ParseResult:
    if result.error:
        return result
    if result.malformed_count:
        result.error = "provider stream contained malformed JSON events"
        result.error_code = "malformed_stream"
    elif result.trailing_count:
        result.error = "provider stream contained records after its terminal event"
        result.error_code = "trailing_stream"
    return result


def parse_opencode(
    lines: Iterable[str], *, strict: bool = False, max_events: int = 10_000,
) -> ParseResult:
    """Parse an OpenCode ``--format json`` JSONL stream.

    Args:
        lines: Iterable of raw JSONL lines (may include malformed/blank lines).

    Returns:
        A :class:`ParseResult`; never raises on malformed input.
    """
    result = ParseResult(source="opencode")
    last_terminal: Optional[str] = None
    for raw in lines:
        result.raw_event_count += 1
        if strict and result.raw_event_count > max_events:
            result.error = f"provider stream exceeded {max_events} events"
            result.error_code = "event_limit"
            return result
        after_terminal = result.saw_terminal
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            result.malformed_count += 1
            if after_terminal:
                result.trailing_count += 1
            continue
        if not isinstance(event, dict):
            if strict:
                result.malformed_count += 1
            if after_terminal:
                result.trailing_count += 1
            continue
        etype = event.get("type")
        if strict:
            part = event.get("part")
            invalid = (
                (etype == OPENCODE_TEXT_EVENT and not (
                    isinstance(part, dict) and part.get("type") in (None, "text")
                    and isinstance(part.get("text"), str)
                ))
                or (etype == "step_finish" and not (
                    isinstance(part, dict) and part.get("type") == "step-finish"
                ))
                or (etype == OPENCODE_ERROR_EVENT and not isinstance(event.get("error"), (dict, str)))
            )
            if invalid:
                result.malformed_count += 1
                if after_terminal:
                    result.trailing_count += 1
                continue
        if after_terminal and not (last_terminal == "error" and etype == "error"):
            result.trailing_count += 1
        if result.session_id is None:
            # Real events carry sessionID at top level; part.sessionID is a
            # tolerated fallback.
            sid = _first_str(event, "sessionID", "session_id", "sessionId")
            part = event.get("part")
            if sid is None and isinstance(part, dict):
                sid = _first_str(part, "sessionID", "session_id", "sessionId")
            result.session_id = sid
        if etype == OPENCODE_TEXT_EVENT:
            _append_opencode_text(result, event)
        elif etype == OPENCODE_ERROR_EVENT:
            result.saw_terminal = True
            result.terminal_count += 1
            last_terminal = "error"
            _record_opencode_error(result, event.get("error"))
        elif etype == "step_finish":
            # A step_finish is terminal only when it ends the *session*.
            # Multi-step sessions emit intermediate step_finish(record) with
            # reason "tool-calls" before the final "stop"; treating the first
            # finish as terminal would misclassify the real answer as trailing.
            part = event.get("part")
            reason = part.get("reason") if isinstance(part, dict) else None
            if reason in (None, "stop", "error", "aborted", "max-turns"):
                result.saw_terminal = True
                result.terminal_count += 1
                last_terminal = "step_finish"
        # step_start / message / session.* / etc. carry no assistant text.
    return _strict_result(result) if strict else result


# ---------------------------------------------------------------------------
# Prime
# ---------------------------------------------------------------------------


def _prime_fingerprint(msg: JsonObject) -> str:
    """Lightweight identity for a Prime message (dedup across event types).

    ``message_end`` and the following ``turn_end`` carry the *same* assistant
    message object; the fingerprint prevents the text from being appended twice
    while still allowing genuinely distinct turns to accumulate.
    """
    content = msg.get("content")
    try:
        content_repr = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        content_repr = repr(content)
    return repr((msg.get("role"), msg.get("stopReason"), msg.get("errorMessage"), content_repr))


def _prime_append_text(result: ParseResult, msg: JsonObject) -> None:
    """Append assistant text from a Prime ``message`` object.

    Tolerates ``content`` as a plain string, a list of plain strings, a list of
    ``{"type":"text","text":...}`` blocks, or a mix -- in order.
    """
    content = msg.get("content")
    if isinstance(content, str):
        result.text += content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, str):
            result.text += block
        elif isinstance(block, dict):
            if block.get("type") not in PRIME_TEXT_BLOCK_TYPES:
                continue
            text = block.get("text")
            if isinstance(text, str):
                result.text += text


def _prime_record_error(result: ParseResult, msg: JsonObject) -> None:
    """Detect a provider/agent error nested in a Prime ``message``.

    Error signals, in priority order:
      1. non-empty ``errorMessage`` (string or nested object with ``message``)
      2. nested ``error`` object with ``message``/``code``
      3. ``stopReason`` in ``{"error", "aborted"}``

    Later messages override earlier ones so the root cause of the final message
    wins.
    """
    stop = msg.get("stopReason")
    error_message = msg.get("errorMessage")
    if isinstance(error_message, dict):
        error_message = _first_str(error_message, "message", "error", "detail")
    if not isinstance(error_message, str) or not error_message:
        error_message = None

    code = None
    error_obj = msg.get("error")
    if isinstance(error_obj, dict):
        if error_message is None:
            error_message = _first_str(error_obj, "message", "error", "detail")
        code = _first_str(error_obj, "code", "name", "errorCode")

    if error_message is None and isinstance(stop, str) and stop in PRIME_ERROR_STOP_REASONS:
        error_message = f"Agent stopped with reason: {stop}"

    if not error_message:
        return
    if code is None:
        code = _first_str(msg, "errorCode", "error_code")
    if code is None and isinstance(stop, str) and stop in PRIME_ERROR_STOP_REASONS:
        code = stop
    result.error = error_message
    result.error_code = code or "provider_error"


def _prime_consume_agent_end(
    result: ParseResult, messages: Any, last_fingerprint: Optional[str]
) -> Optional[str]:
    """Handle the ``agent_end`` event: scan for errors and salvage text.

    The ``messages`` array is not appended wholesale (its assistant message is
    normally the same object already consumed from ``message_end``). We only
    append the *last* assistant message when its fingerprint is new, which covers
    streams where ``message_end`` was malformed/missing.
    """
    if not isinstance(messages, list):
        return last_fingerprint
    for m in messages:
        if isinstance(m, dict):
            _prime_record_error(result, m)
    for m in reversed(messages):
        if isinstance(m, dict) and (m.get("role") == "assistant" or m.get("role") is None):
            fp = _prime_fingerprint(m)
            if fp != last_fingerprint:
                last_fingerprint = fp
                _prime_append_text(result, m)
            break
    return last_fingerprint


def parse_prime(
    lines: Iterable[str], *, strict: bool = False, max_events: int = 10_000,
) -> ParseResult:
    """Parse a Prime Agent ``--mode json`` JSONL stream.

    Args:
        lines: Iterable of raw JSONL lines (may include malformed/blank lines).

    Returns:
        A :class:`ParseResult`; never raises on malformed input.
    """
    result = ParseResult(source="prime")
    last_fingerprint: Optional[str] = None
    for raw in lines:
        result.raw_event_count += 1
        if strict and result.raw_event_count > max_events:
            result.error = f"provider stream exceeded {max_events} events"
            result.error_code = "event_limit"
            return result
        after_terminal = result.saw_terminal
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            result.malformed_count += 1
            if after_terminal:
                result.trailing_count += 1
            continue
        if not isinstance(event, dict):
            if strict:
                result.malformed_count += 1
            if after_terminal:
                result.trailing_count += 1
            continue
        etype = event.get("type")
        if strict:
            invalid = (
                (etype == "agent_end" and not isinstance(event.get("messages"), list))
                or (etype in ("message_end", "turn_end") and not isinstance(event.get("message"), dict))
            )
            if invalid:
                result.malformed_count += 1
                if after_terminal:
                    result.trailing_count += 1
                continue
        if after_terminal:
            result.trailing_count += 1
        if result.session_id is None:
            result.session_id = _first_str(event, "sessionId", "session_id", "sessionID")

        if etype == "agent_end":
            result.saw_terminal = True
            result.terminal_count += 1
            last_fingerprint = _prime_consume_agent_end(
                result, event.get("messages"), last_fingerprint
            )
        elif etype in ("message_end", "turn_end"):
            msg = event.get("message")
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "assistant" or msg.get("role") is None:
                fp = _prime_fingerprint(msg)
                if fp != last_fingerprint:
                    last_fingerprint = fp
                    _prime_append_text(result, msg)
            # Errors are scanned on every message (user, toolResult, ...).
            _prime_record_error(result, msg)
            if result.session_id is None:
                result.session_id = _first_str(msg, "sessionId", "session_id", "sessionID")
        # Everything else (agent_start, turn_start, message_start,
        # message_update, tool_execution_*) carries deltas or lifecycle info.
    return _strict_result(result) if strict else result


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def parse(format: str, lines: Iterable[str], *, strict: bool = False) -> ParseResult:
    """Parse a JSONL event stream for the given ``format``.

    Args:
        format: ``"opencode"`` or ``"prime"``.
        lines: Iterable of raw JSONL lines.

    Raises:
        ValueError: for an unknown format name.
    """
    if format == "opencode":
        return parse_opencode(lines, strict=strict)
    if format == "prime":
        return parse_prime(lines, strict=strict)
    raise ValueError(f"unknown event format: {format!r} (expected 'opencode' or 'prime')")


def parse_text(format: str, text: str, *, strict: bool = False) -> ParseResult:
    """Parse a whole JSONL blob (newline separated) for the given ``format``."""
    if text is None:
        text = ""
    return parse(format, io.StringIO(text), strict=strict)
