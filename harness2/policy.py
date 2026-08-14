"""Deterministic, privacy-aware engine and expert routing policy."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Dict, Optional

from .models import EngineStatus, RoutingDecision, RunRequest
from .execution import ProcessConfigurationError, prepare_working_directory


_DURABLE = ("long-running", "long running", "background agent", "persistent", "ipython", "recursive subagent", "schedule", "heartbeat", "detach", "reattach", "rlm")
_MESSAGING = ("telegram", "discord", "whatsapp", "signal", "send message", "broadcast", "gateway")
_PARALLEL = ("parallel agents", "fan out", "fan-out", "delegate in parallel", "multiple workers")


@dataclass(frozen=True)
class _PreparedRunRequest(RunRequest):
    """Internal request carrying filesystem authority established by policy."""

    cwd_identity: tuple[int, int] = (0, 0)


class PolicyRefusal(RuntimeError):
    pass


def _inside(path: Optional[str], root: str) -> bool:
    if not path:
        return False
    try:
        canonical_root = os.path.realpath(root)
        return os.path.commonpath((path, canonical_root)) == canonical_root
    except ValueError:
        return False


def guarded_roots() -> tuple[str, ...]:
    configured = os.environ.get("HARNESS_GUARDED_ROOTS")
    return tuple(
        os.path.abspath(os.path.expanduser(path))
        for path in (configured or "").split(os.pathsep)
        if path
    )


def canonicalize_request(request: RunRequest) -> RunRequest:
    try:
        if isinstance(request, _PreparedRunRequest):
            cwd, identity = prepare_working_directory(request.cwd, request.cwd_identity)
            return request
        cwd, identity = prepare_working_directory(request.cwd)
    except ProcessConfigurationError as exc:
        raise PolicyRefusal(str(exc)) from exc
    values = {item.name: getattr(request, item.name) for item in fields(RunRequest)}
    values["cwd"] = cwd
    return _PreparedRunRequest(**values, cwd_identity=identity)


def restore_request_authority(request: RunRequest, identity: tuple[int, int]) -> RunRequest:
    """Reconstitute only authenticated persisted CWD authority."""

    values = {item.name: getattr(request, item.name) for item in fields(RunRequest)}
    prepared = _PreparedRunRequest(**values, cwd_identity=identity)
    return canonicalize_request(prepared)


def expert_for_task(task: str) -> tuple[str, Optional[str], str]:
    """Return neutral configured defaults; external routers are not trusted code.

    The former compatibility path imported ``HARNESS_ROUTER`` Python directly
    into the control process. v3 fails closed instead. A future router must use
    a bounded, credential-free typed worker protocol.
    """
    del task
    return (
        os.environ.get("HARNESS_DEFAULT_AGENT") or None,
        os.environ.get("HARNESS_DEFAULT_MODEL") or None,
        "trusted deterministic routing; no external router executed",
    )
class PolicyRouter:
    def __init__(self, statuses: Dict[str, EngineStatus]):
        self.statuses = statuses

    def _usable(self, name: str) -> bool:
        status = self.statuses.get(name)
        return bool(status and status.available and status.enabled)

    def decide(self, request: RunRequest) -> RoutingDecision:
        request = canonicalize_request(request)
        cwd = request.cwd
        for root in guarded_roots():
            if _inside(cwd, root):
                raise PolicyRefusal(
                    f"execution inside guarded root {root} is blocked; Harness will not launch a worker there"
                )

        explicit = request.engine != "auto"
        text = request.prompt.lower()
        if request.sensitive:
            if not self._usable("local") or not self.statuses["local"].healthy:
                raise PolicyRefusal("sensitive tasks require an enabled, healthy loopback local engine")
            return RoutingDecision("local", None, request.model, "sensitive policy: loopback only", (), "sensitive")

        if request.untrusted:
            if explicit and request.engine not in ("opencode", "auto"):
                raise PolicyRefusal("untrusted tasks may only use the read-only OpenCode sandbox agent")
            return RoutingDecision(
                "opencode", "harness-sandbox", request.model,
                "untrusted policy: plugins off, read-only sandbox agent", (), "untrusted",
            )

        agent, routed_model, expert_reason = expert_for_task(request.prompt)
        model = request.model or routed_model

        if explicit:
            if request.engine not in self.statuses:
                raise PolicyRefusal(f"unknown engine: {request.engine}")
            if not self._usable(request.engine):
                raise PolicyRefusal(f"engine {request.engine} is unavailable or disabled")
            if request.engine == "hermes" and (request.sensitive or request.untrusted):
                raise PolicyRefusal("Hermes task text is argv-visible on this installation; sensitive/untrusted use is refused")
            chosen_agent = agent if request.engine in ("opencode", "zen") else None
            return RoutingDecision(
                request.engine, request.agent or chosen_agent, request.model or model,
                "explicit engine selection", (), "explicit",
            )

        if any(token in text for token in _MESSAGING):
            if not self._usable("hermes"):
                raise PolicyRefusal("messaging task requires Hermes; no silent fallback is allowed")
            return RoutingDecision("hermes", None, None, "messaging capability required", (), "messaging")

        if any(token in text for token in _PARALLEL):
            if self._usable("hermes"):
                return RoutingDecision("hermes", None, None, "parallel worker policy", ("opencode",), "parallel")

        if any(token in text for token in _DURABLE):
            if self._usable("prime"):
                return RoutingDecision("prime", None, request.model, "durable/RLM capability required", ("opencode",), "durable")

        if not self._usable("opencode"):
            if self._usable("prime"):
                return RoutingDecision("prime", None, request.model, "OpenCode unavailable", (), "fallback")
            raise PolicyRefusal("no healthy external engine is available")

        return RoutingDecision(
            "opencode", request.agent or agent, request.model or model,
            "OpenCode control-plane policy; " + expert_reason, (), "control",
        )
