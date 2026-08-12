"""Deterministic, privacy-aware engine and expert routing policy."""

from __future__ import annotations

import importlib.util
import os
from typing import Dict, Optional

from .models import EngineStatus, RoutingDecision, RunRequest


_DURABLE = ("long-running", "long running", "background agent", "persistent", "ipython", "recursive subagent", "schedule", "heartbeat", "detach", "reattach", "rlm")
_MESSAGING = ("telegram", "discord", "whatsapp", "signal", "send message", "broadcast", "gateway")
_PARALLEL = ("parallel agents", "fan out", "fan-out", "delegate in parallel", "multiple workers")


class PolicyRefusal(RuntimeError):
    pass


def _inside(path: Optional[str], root: str) -> bool:
    if not path:
        return False
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


def guarded_roots() -> tuple[str, ...]:
    configured = os.environ.get("HARNESS_GUARDED_ROOTS")
    return tuple(
        os.path.abspath(os.path.expanduser(path))
        for path in (configured or "").split(os.pathsep)
        if path
    )


def expert_for_task(task: str) -> tuple[str, Optional[str], str]:
    """Invoke the Genius classifier in-process, always dry-run/non-mutating."""
    path = os.environ.get(
        "HARNESS_ROUTER",
        os.path.join(os.path.expanduser("~"), ".config", "opencode", "skills", "genius-core", "genius_router.py"),
    )
    try:
        spec = importlib.util.spec_from_file_location("harness2_genius_router", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        decision = module.AutoSwitcher().route(task, dry_run=True)
        return decision.recommended_agent, decision.recommended_model, decision.reasoning
    except Exception as exc:
        return os.environ.get("HARNESS_DEFAULT_AGENT") or None, os.environ.get("HARNESS_DEFAULT_MODEL") or None, f"expert classifier unavailable: {exc}"


class PolicyRouter:
    def __init__(self, statuses: Dict[str, EngineStatus]):
        self.statuses = statuses

    def _usable(self, name: str) -> bool:
        status = self.statuses.get(name)
        return bool(status and status.available and status.enabled)

    def decide(self, request: RunRequest) -> RoutingDecision:
        cwd = request.cwd or os.getcwd()
        for root in guarded_roots():
            if _inside(cwd, root):
                raise PolicyRefusal(
                    f"execution inside guarded root {root} is blocked; Harness will not launch a worker there"
                )

        explicit = request.engine != "auto"
        text = request.prompt.lower()
        agent, routed_model, expert_reason = expert_for_task(request.prompt)
        model = request.model or routed_model

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
