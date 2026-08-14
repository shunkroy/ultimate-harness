"""Provider-fluid candidate routing: eligibility, ordering and scoring seams.

The central router must not permanently know every AI vendor. It knows the
contract a runtime satisfies: engines expose :class:`RuntimeManifest` data and
a status; candidates are assembled from *capability-compatible usable*
engines, filtered by health/auth/policy, ordered by a deterministic
preference, and scored through an extensible hook. Today the default order is
data (env ``HARNESS_FALLBACK_ORDER`` or a small preference table) -- not a
vendor chain baked into routing logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Tuple

from .models import EngineStatus

#: Default engine preference (execution runtimes, not vendors). Free healthy
#: routes win at scoring time via cost_class; capability fit adjusts score.
#: ``direct`` is the raw REST single-shot route: no agent boot, sub-second
#: Q&A; it rescues AUTO when an agent engine fails (measured rescue path).
DEFAULT_ENGINE_PREFERENCE: Tuple[str, ...] = ("opencode", "prime", "zen", "direct", "hermes", "local")


def fallback_order() -> Tuple[str, ...]:
    """Return the engine preference order (env override first)."""
    configured = os.environ.get("HARNESS_FALLBACK_ORDER")
    if configured:
        ordered = tuple(name.strip() for name in configured.split(",") if name.strip())
        if ordered:
            return ordered
    return DEFAULT_ENGINE_PREFERENCE


@dataclass(frozen=True)
class Candidate:
    engine: str
    reason: str
    score: float = 0.0


@dataclass(frozen=True)
class CandidateRoute:
    """The ordered candidate route plus the engines that were skipped."""

    candidates: Tuple[str, ...] = ()
    skipped: Tuple[Tuple[str, str], ...] = ()

    def as_metadata(self) -> Dict[str, object]:
        return {
            "candidate_routes": list(self.candidates),
            "skipped_routes": [{"engine": name, "reason": reason} for name, reason in self.skipped],
        }


def assemble_candidates(
    primary: str,
    statuses: Dict[str, EngineStatus],
    *,
    capability: Optional[str] = None,
) -> CandidateRoute:
    """Build the ordered eligible route starting at ``primary``.

    Engines that are unknown, disabled, unavailable or unhealthy are skipped
    with a machine-readable reason. ``capability`` (e.g. ``"reason.general"``)
    is reserved for capability-based filtering seams.
    """
    order = fallback_order()
    candidates: list[str] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()

    def consider(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        if name not in statuses:
            skipped.append((name, "unknown engine"))
            return
        status = statuses[name]
        if not status.enabled:
            skipped.append((name, f"disabled: {status.detail or 'not enabled'}"))
            return
        if not status.available:
            skipped.append((name, f"unavailable: {status.detail or ''}"))
            return
        if not status.healthy:
            skipped.append((name, f"unhealthy: {status.detail or ''}"))
            return
        if capability and capability not in status.capabilities:
            skipped.append((name, f"missing capability {capability}"))
            return
        candidates.append(name)

    consider(primary)
    for name in order:
        consider(name)
    return CandidateRoute(tuple(candidates), tuple(skipped))


def score_candidate(
    engine: str,
    status: Optional[EngineStatus],
    *,
    circuit_allowed: Optional[bool] = None,
    cost_class: Optional[str] = None,
    capability_fit: Optional[str] = None,
    user_preference: Optional[Sequence[str]] = None,
) -> float:
    """Deterministic candidate score (extensible seam).

    Weights are intentionally flat defaults; future scoring may weight
    latency, context capacity, privacy and reliability. The seam exists so
    ranking logic never hard-codes vendor names.
    """
    score = 10.0
    if circuit_allowed is False:
        score -= 20.0
    if cost_class == "free":
        score += 2.0
    elif cost_class == "subscription":
        score += 0.5
    if capability_fit:
        score += 1.5
    if user_preference and engine in user_preference:
        score += 1.0
    return score


def rank_fallbacks(
    candidates: Sequence[str],
    statuses: Dict[str, EngineStatus],
    *,
    circuit_allowed: Optional[Dict[str, bool]] = None,
    capability_fit: Optional[Dict[str, str]] = None,
    user_preference: Optional[Sequence[str]] = None,
) -> Tuple[str, ...]:
    """Order fallback candidates by score (stable; primary excluded).

    ``circuit_allowed`` maps engine -> whether its breaker allows a call;
    ``capability_fit`` maps engine -> capability token that scored a fit;
    both are optional seam inputs.
    """
    if not candidates:
        return ()
    ranked = []
    for index, name in enumerate(candidates):
        status = statuses.get(name)
        ranked.append((
            score_candidate(
                name, status,
                circuit_allowed=(circuit_allowed or {}).get(name),
                cost_class=getattr(status, "cost_class", None) if status is not None else None,
                capability_fit=(capability_fit or {}).get(name),
                user_preference=user_preference,
            ),
            index, name,
        ))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return tuple(item[2] for item in ranked)
