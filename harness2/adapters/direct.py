"""Direct REST engine: sub-second single-shot Q&A, no agent boot.

Why this engine exists: the agent engines (opencode/prime) boot a full agent
session per run. Measured on the phone (2026-08-14): ~44s boot plus ~12.6k
input tokens before the first model token -- while a raw REST call to the
same providers completes in ~1.4s. For single-shot questions, the direct
engine performs a raw HTTP call (stdlib ``urllib`` only) straight to the
provider API.

Providers are addressed by model-id prefix and their key is read from the
environment at call time (never stored, never logged):

    groq/<model>     -> GROQ_API_KEY     (OpenAI-compatible /chat/completions)
    google/<model>   -> GEMINI_API_KEY   (generativelanguage generateContent)
    deepseek/<model> -> DEEPSEEK_API_KEY (OpenAI-compatible /chat/completions)

Failure handling stays inside the harness taxonomy: raw HTTP codes and body
snippets are passed through as ``error_code``/``error`` and the orchestrator
normalizes them via :func:`harness2.events.normalize_failure` (429 ->
rate_limited, 401/403 -> authentication_failed, 402/quota text ->
quota_exhausted, 5xx -> provider_unavailable, socket timeout -> timeout).
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Dict, Optional, Tuple

from ..models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from .base import EngineAdapter
from .manifest import COST_FREE, PRIVACY_EXTERNAL, RuntimeManifest, manifest_from_status

#: provider prefix -> (env var name, OpenAI-compatible vs google-shaped API)
_PROVIDERS: Dict[str, Tuple[str, bool]] = {
    "groq": ("GROQ_API_KEY", True),
    "deepseek": ("DEEPSEEK_API_KEY", True),
    "google": ("GEMINI_API_KEY", False),
}

_GOOGLE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_OPENAI_ENDPOINTS: Dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
}

#: Some providers edge-protect against generic HTTP client signatures
#: (observed: Groq CDN refused urllib's default Python-urllib UA with HTTP
#: 1010 while curl succeeded). A descriptive client UA is used instead.
USER_AGENT = "harness-direct/3.0.0 (+https://github.com/shunkroy/ultimate-harness)"

#: known models (verified live where marked); unknown models are attempted
#: against their provider with the raw name.
KNOWN_MODELS: Dict[str, str] = {
    "groq/llama-3.3-70b-versatile": "verified 2026-08-14 (1.4s)",
    "groq/llama-3.1-8b-instant": "known",
    "google/gemini-3.5-flash-lite": "verified 2026-08-14 (1.4s)",
    "deepseek/deepseek-chat": "known; balance-limited on this account",
}


class DirectAdapter(EngineAdapter):
    """Raw REST single-shot engine (no agent loop, sub-second)."""

    name = "direct"

    def __init__(self, config) -> None:
        self._config = config

    # -- provider plumbing -------------------------------------------------
    @staticmethod
    def _key_for(prefix: str) -> Optional[str]:
        env_name = _PROVIDERS[prefix][0]
        return os.environ.get(env_name)

    @classmethod
    def _any_key(cls) -> bool:
        return any(cls._key_for(prefix) is not None for prefix in _PROVIDERS)

    # -- adapter contract --------------------------------------------------
    def status(self) -> EngineStatus:
        available = self._any_key()
        return EngineStatus(
            self.name, available, True, available,
            CapabilityStatus.ACTIVE if available else CapabilityStatus.IMPLEMENTED,
            "" if available else "no direct provider key configured",
            ("reason.general",),
            PRIVACY_EXTERNAL,
            COST_FREE,
        )

    def manifest(self) -> RuntimeManifest:
        manifest = manifest_from_status(self.status())
        return replace(
            manifest,
            models=tuple(KNOWN_MODELS),
            cost_class=COST_FREE,
            privacy_class=PRIVACY_EXTERNAL,
            evidence=(
                "direct REST via stdlib urllib; no agent boot; "
                "measured ~1.4s per request (groq + google, 2026-08-14)",
            ),
        )

    def run(self, request: RunRequest) -> EngineResult:
        started = time.monotonic()
        error_result = lambda **kw: EngineResult(  # noqa: E731
            self.name, False, duration=time.monotonic() - started, exit_code=1, **kw
        )
        if not request.model:
            return error_result(
                error="direct engine requires --model (e.g. groq/llama-3.3-70b-versatile)",
                error_code="invalid_request",
            )
        prefix, _, model_name = request.model.partition("/")
        if prefix not in _PROVIDERS:
            return error_result(
                error=f"unknown direct provider prefix {prefix!r} (use groq/, google/, deepseek/)",
                error_code="unknown_provider_failure",
            )
        key = self._key_for(prefix)
        if not key:
            return error_result(
                error=f"{_PROVIDERS[prefix][0]} key is not configured",
                error_code="missing_credential",
            )
        try:
            text, raw = self._call(prefix, model_name, key, request.prompt, request.timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read(400).decode("utf-8", "replace").strip()
            return error_result(
                error=body or exc.reason or str(exc),
                error_code=str(exc.code),
                metadata={"http_status": exc.code, "provider": prefix},
            )
        except socket.timeout:
            return error_result(
                error="direct provider request timed out",
                error_code="timeout",
            )
        except urllib.error.URLError as exc:
            reason = exc.reason if exc.reason is not None else str(exc)
            return error_result(
                error=f"direct provider network error: {reason}",
                error_code="network_failure",
            )
        except (ValueError, KeyError) as exc:
            return error_result(
                error=f"unexpected direct provider response: {exc}",
                error_code="unknown_provider_failure",
            )
        return EngineResult(
            self.name, True, text=text, exit_code=0,
            duration=time.monotonic() - started,
        )

    # -- HTTP --------------------------------------------------------------
    def _call(
        self, prefix: str, model_name: str, key: str, prompt: str, timeout: int,
    ) -> Tuple[str, str]:
        openai_compat = _PROVIDERS[prefix][1]
        if openai_compat:
            url = _OPENAI_ENDPOINTS[prefix]
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
            }
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            return self._post(url, payload, headers, timeout, openai_compat=True)
        url = _GOOGLE_ENDPOINT.format(model=model_name) + f"?key={key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 2048},
        }
        return self._post(url, payload, {"Content-Type": "application/json"}, timeout, openai_compat=False)

    @staticmethod
    def _post(
        url: str, payload: Dict[str, object], headers: Dict[str, str],
        timeout: int, *, openai_compat: bool,
    ) -> Tuple[str, str]:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers={**headers, "User-Agent": USER_AGENT}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=max(5, min(int(timeout), 120))) as response:
            raw = response.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSON from provider ({exc}); raw={raw[:200]!r}"
            ) from exc
        if openai_compat:
            text = data["choices"][0]["message"]["content"]
            raw_meta = data.get("usage", {}) if isinstance(data, dict) else {}
        else:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
            raw_meta = {}
        if not isinstance(text, str) or not text.strip():
            raise ValueError("provider returned empty completion")
        return text.strip(), json.dumps(raw_meta)[:400]