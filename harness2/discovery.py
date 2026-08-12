"""Bounded, declarative and non-destructive local provider discovery."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence, Tuple

from .kernel.contracts import (
    CapabilityEvidence,
    EvidenceKind,
    Health,
    Maturity,
    RuntimeDescriptor,
)


@dataclass(frozen=True)
class CliProbeSpec:
    id: str
    display_name: str
    executable: Optional[str]
    version_args: Tuple[str, ...]
    interface: str
    input_mode: str
    output_mode: str
    capabilities: Tuple[str, ...]
    auth_names: Tuple[str, ...] = ()
    limitations: Tuple[str, ...] = ()
    timeout: int = 5

    def __post_init__(self) -> None:
        if not self.id or not self.display_name:
            raise ValueError("CLI probe requires id and display name")
        if not self.version_args or self.timeout < 1 or self.timeout > 30:
            raise ValueError("CLI probe requires bounded version arguments and timeout")


_VERSION = re.compile(r"(?<!\d)(\d+\.\d+(?:\.\d+)?(?:[-+._a-zA-Z0-9]*)?)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version(text: str) -> Optional[str]:
    match = _VERSION.search(text)
    return match.group(1) if match else None


def probe_cli(spec: CliProbeSpec, *, env: Optional[Mapping[str, str]] = None) -> RuntimeDescriptor:
    path = os.path.abspath(spec.executable) if spec.executable else None
    present = bool(path and os.path.isfile(path) and os.access(path, os.X_OK))
    evidence = CapabilityEvidence(
        EvidenceKind.LOCAL_OBSERVATION,
        path or f"PATH:{spec.id}",
        _now(),
        "executable present" if present else "executable unavailable",
    )
    if not present:
        return RuntimeDescriptor(
            spec.id, "cli", spec.display_name, None, path, spec.interface,
            spec.input_mode, spec.output_mode, spec.capabilities, spec.auth_names,
            spec.limitations + ("executable unavailable",), Maturity.DESIGNED,
            False, Health.DOWN, (evidence,),
        )
    try:
        proc = subprocess.run(
            [path, *spec.version_args], capture_output=True, text=True,
            timeout=spec.timeout, env=dict(env) if env is not None else None,
        )
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[:500]
        healthy = proc.returncode == 0
        observed = CapabilityEvidence(
            EvidenceKind.LOCAL_OBSERVATION, path, _now(),
            f"version probe exit={proc.returncode}",
        )
        return RuntimeDescriptor(
            spec.id, "cli", spec.display_name, _version(text), path,
            spec.interface, spec.input_mode, spec.output_mode, spec.capabilities,
            spec.auth_names, spec.limitations, Maturity.IMPLEMENTED, True,
            Health.HEALTHY if healthy else Health.DEGRADED,
            (evidence, observed),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        failed = CapabilityEvidence(
            EvidenceKind.LOCAL_OBSERVATION, path, _now(),
            f"version probe failed: {type(exc).__name__}",
        )
        return RuntimeDescriptor(
            spec.id, "cli", spec.display_name, None, path, spec.interface,
            spec.input_mode, spec.output_mode, spec.capabilities, spec.auth_names,
            spec.limitations + ("version probe failed",), Maturity.IMPLEMENTED,
            True, Health.DEGRADED, (evidence, failed),
        )


def default_specs(config) -> tuple[CliProbeSpec, ...]:
    return (
        CliProbeSpec(
            "opencode", "OpenCode", config.opencode_bin, ("--version",),
            "cli", "private_file", "jsonl",
            ("reason.general", "code.execute", "research.general"),
            ("OPENAI_API_KEY", "GEMINI_API_KEY", "OPENCODE_API_KEY"),
            timeout=20,
        ),
        CliProbeSpec(
            "prime", "Prime Agent", config.prime_launcher, ("--dist", "--version"),
            "cli", "private_file", "jsonl",
            ("reason.general", "code.execute", "agent.durable", "agent.recursive"),
            ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"),
            ("persistent kernel readiness requires a separate health probe",),
            timeout=15,
        ),
        CliProbeSpec(
            "hermes", "Hermes Agent", config.hermes_bin, ("--version",),
            "cli", "argv", "text",
            ("reason.general", "message.send", "agent.parallel"),
            (), ("task input is argv-visible", "configured provider requires independent authorization", "disabled by default"),
            timeout=10,
        ),
    )


def discover(config) -> tuple[RuntimeDescriptor, ...]:
    env = config.clean_env("local")
    return tuple(probe_cli(spec, env=env) for spec in default_specs(config))
