"""Disabled-by-default loopback llama.cpp adapter."""

from __future__ import annotations

import json
import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import EngineAdapter
from ..config import HarnessConfig
from ..models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from ..store import Store
from ..platforms import is_loopback_url
from ..execution import BodyDeadlineExceeded, BodyLimitExceeded, read_bounded_body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, *, timeout: float):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


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
            with _open(self.config.local_url + "/models", timeout=3) as response:
                final_url = response.geturl()
                return response.status == 200 and is_loopback_url(final_url)
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
        fingerprint = hashlib.sha256(json.dumps({
            "schema": "harness.local-execution/v1",
            "url": self.config.local_url,
            "model": request.model or "local",
            "timeout": request.timeout,
            "body_limit": self.config.http_body_limit,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
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
            with _open(req, timeout=request.timeout) as response:
                if not is_loopback_url(response.geturl()):
                    return EngineResult(
                        self.name, False, error="local endpoint redirected away from loopback",
                        error_code="unsafe_endpoint", exit_code=2,
                        duration=time.monotonic() - started,
                        metadata={"execution_config_sha256": fingerprint},
                    )
                data = json.loads(read_bounded_body(
                    response, byte_limit=int(self.config.http_body_limit),
                    deadline=started + request.timeout,
                ).decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
            return EngineResult(
                self.name, True, str(text), exit_code=0,
                duration=time.monotonic() - started,
                metadata={"execution_config_sha256": fingerprint},
            )
        except BodyDeadlineExceeded as exc:
            return EngineResult(
                self.name, False, error=str(exc), error_code="timeout",
                exit_code=124, duration=time.monotonic() - started,
                metadata={"execution_config_sha256": fingerprint},
            )
        except BodyLimitExceeded as exc:
            return EngineResult(
                self.name, False, error=str(exc), error_code="output_limit",
                exit_code=125, duration=time.monotonic() - started,
                metadata={"execution_config_sha256": fingerprint},
            )
        except (urllib.error.URLError, KeyError, IndexError, ValueError) as exc:
            return EngineResult(
                self.name, False, error=str(exc), error_code="local_error",
                exit_code=1, duration=time.monotonic() - started,
                metadata={"execution_config_sha256": fingerprint},
            )
