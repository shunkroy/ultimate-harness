"""Disabled-by-default loopback llama.cpp adapter."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import EngineAdapter
from ..config import HarnessConfig
from ..models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from ..store import Store
from ..platforms import is_loopback_url


class LocalAdapter(EngineAdapter):
    name = "local"

    def __init__(self, config: HarnessConfig, store: Store):
        self.config = config
        self.store = store

    def enabled(self) -> bool:
        return self.store.setting("engine.local.enabled", "false") == "true"

    def set_enabled(self, enabled: bool) -> None:
        self.store.set_setting("engine.local.enabled", "true" if enabled else "false")

    def _loopback(self) -> bool:
        return is_loopback_url(self.config.local_url)

    def _probe(self) -> bool:
        if not self._loopback():
            return False
        try:
            with urllib.request.urlopen(self.config.local_url + "/models", timeout=3) as response:
                return response.status == 200
        except Exception:
            return False

    def status(self) -> EngineStatus:
        enabled = self.enabled()
        healthy = enabled and self._probe()
        return EngineStatus(
            self.name, True, healthy, enabled,
            CapabilityStatus.ACTIVE if healthy else CapabilityStatus.IMPLEMENTED,
            "loopback server ready" if healthy else ("enabled; server down" if enabled else "disabled by policy"),
            ("offline", "privacy"),
            "loopback-body",
        )

    def run(self, request: RunRequest) -> EngineResult:
        started = time.monotonic()
        if not self.enabled():
            return EngineResult(self.name, False, error="local engine is disabled", error_code="disabled", exit_code=2)
        if not self._loopback():
            return EngineResult(self.name, False, error="local endpoint is not loopback", error_code="unsafe_endpoint", exit_code=2)
        body = json.dumps({
            "model": request.model or "local", "messages": [{"role": "user", "content": request.prompt}],
            "max_tokens": 1024,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.config.local_url + "/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=request.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
            return EngineResult(self.name, True, str(text), exit_code=0, duration=time.monotonic() - started)
        except (urllib.error.URLError, KeyError, IndexError, ValueError) as exc:
            return EngineResult(self.name, False, error=str(exc), error_code="local_error", exit_code=1, duration=time.monotonic() - started)
