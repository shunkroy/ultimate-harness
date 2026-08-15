"""Direct REST engine: status, manifests, REST calls, error mapping."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
import unittest.mock as mock
import urllib.error

from harness2.adapters.base import EngineAdapter
from harness2.adapters.direct import DEFAULT_MODEL, DirectAdapter, KNOWN_MODELS
from harness2.events import (
    FAILURE_AUTHENTICATION,
    FAILURE_RATE_LIMITED,
    normalize_failure,
)
from harness2.legacy_bridge import GovernedLegacyRuntimeDriver, execution_request_from_legacy
from harness2.models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from harness2.router import assemble_candidates

from .test_provider_routing import statuses


def _statuses_with_direct(enabled=True):
    seen = statuses()
    seen["direct"] = EngineStatus(
        "direct", enabled, enabled, enabled,
        CapabilityStatus.ACTIVE if enabled else CapabilityStatus.IMPLEMENTED,
        "" if enabled else "no direct provider key configured",
        ("reason.general",), "external", "free",
    )
    return seen


def _fake_adapter(keys=("GROQ_API_KEY",)):
    with mock.patch.dict(os.environ, {k: "test-key" for k in keys}, clear=True):
        adapter = DirectAdapter(None)
    return adapter


def _fake_openai_response(text="DIRECT-OK"):
    return json.dumps({
        "choices": [{"message": {"content": text}}],
        "usage": {"total_tokens": 42},
    }).encode()


def _fake_google_response(text="DIRECT-OK"):
    return json.dumps({
        "candidates": [{"content": {"parts": [{"text": text}]}}],
    }).encode()


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return self._payload[:limit] if limit > 0 else self._payload


class DirectStatusTests(unittest.TestCase):
    def test_disabled_without_any_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            status = DirectAdapter(None).status()
        self.assertFalse(status.enabled)
        self.assertFalse(status.available)
        self.assertIn("no direct provider key", status.detail)

    def test_enabled_when_any_key_present(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}, clear=True):
            status = DirectAdapter(None).status()
        self.assertTrue(status.enabled)
        self.assertTrue(status.healthy)

    def test_manifest_lists_verified_models_and_free_cost(self):
        manifest = _fake_adapter().manifest()
        self.assertIn("groq/llama-3.3-70b-versatile", manifest.models)
        self.assertIn("google/gemini-3.5-flash-lite", manifest.models)
        self.assertEqual(manifest.cost_class, "free")
        self.assertTrue(manifest.evidence)

    def test_router_skips_direct_when_unconfigured(self):
        route = assemble_candidates("opencode", _statuses_with_direct(enabled=False))
        self.assertNotIn("direct", route.candidates)
        self.assertTrue(any(name == "direct" and "disabled" in reason for name, reason in route.skipped))

    def test_router_includes_direct_when_configured(self):
        route = assemble_candidates("opencode", _statuses_with_direct(enabled=True))
        self.assertIn("direct", route.candidates)


class DirectRunTests(unittest.TestCase):
    def test_groq_success(self):
        adapter = _fake_adapter()
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        return_value=FakeResponse(_fake_openai_response("GROQ-OK"))) as urlopen:
            result = adapter.run(RunRequest("hello", model="groq/llama-3.3-70b-versatile"))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.text, "GROQ-OK")
        url = urlopen.call_args.args[0].full_url
        self.assertIn("api.groq.com", url)

    def test_google_success(self):
        adapter = _fake_adapter(("GEMINI_API_KEY",))
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        return_value=FakeResponse(_fake_google_response("GM-OK"))):
            result = adapter.run(RunRequest("hello", model="google/gemini-3.5-flash-lite"))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.text, "GM-OK")

    def test_deepseek_requires_own_key(self):
        adapter = _fake_adapter(("GROQ_API_KEY",))
        result = adapter.run(RunRequest("hello", model="deepseek/deepseek-chat"))
        self.assertFalse(result.success)
        self.assertEqual(normalize_failure(result.error_code, result.error), FAILURE_AUTHENTICATION)

    def test_missing_model_falls_back_to_builtin_default(self):
        adapter = _fake_adapter()
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        return_value=FakeResponse(_fake_openai_response("DEF-OK"))) as urlopen:
            with mock.patch.dict(os.environ, {}, clear=False):
                result = adapter.run(RunRequest("hello"))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.text, "DEF-OK")
        self.assertEqual(result.metadata["resolved_model"], DEFAULT_MODEL)
        url = urlopen.call_args.args[0].full_url
        self.assertIn("groq.com", url)

    def test_env_default_model_wins_over_builtin(self):
        adapter = _fake_adapter(("GEMINI_API_KEY",))
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        return_value=FakeResponse(_fake_google_response("GM-OK"))):
            with mock.patch.dict(os.environ,
                                 {"HARNESS_DEFAULT_MODEL": "google/gemini-3.5-flash-lite"},
                                 clear=False):
                result = adapter.run(RunRequest("hello"))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["resolved_model"], "google/gemini-3.5-flash-lite")

    def test_explicit_model_beats_env_default(self):
        adapter = _fake_adapter()
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        return_value=FakeResponse(_fake_openai_response("EXP-OK"))):
            with mock.patch.dict(os.environ,
                                 {"HARNESS_DEFAULT_MODEL": "google/gemini-3.5-flash-lite"},
                                 clear=False):
                result = adapter.run(RunRequest("hello", model="groq/llama-3.3-70b-versatile"))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["resolved_model"], "groq/llama-3.3-70b-versatile")

    def test_device_context_sent_to_openai_provider(self):
        adapter = _fake_adapter()
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        return_value=FakeResponse(_fake_openai_response("CTX-OK"))) as urlopen:
            result = adapter.run(RunRequest("what time is it", model="groq/llama-3.3-70b-versatile"))
        self.assertTrue(result.success, result.error)
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertIn("local time", sent["messages"][0]["content"])

    def test_device_context_sent_to_google_provider(self):
        adapter = _fake_adapter(("GEMINI_API_KEY",))
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        return_value=FakeResponse(_fake_google_response("CTX-OK"))) as urlopen:
            result = adapter.run(RunRequest("what time is it", model="google/gemini-3.5-flash-lite"))
        self.assertTrue(result.success, result.error)
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertIn("system_instruction", sent)
        self.assertIn("local time", sent["system_instruction"]["parts"][0]["text"])

    def test_unknown_prefix(self):
        adapter = _fake_adapter()
        result = adapter.run(RunRequest("hello", model="anthropic/claude-x"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "unknown_provider_failure")

    def test_http_401_maps_to_authentication(self):
        adapter = _fake_adapter()
        exc = urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen", side_effect=exc):
            result = adapter.run(RunRequest("hello", model="groq/llama-3.3-70b-versatile"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "401")
        self.assertEqual(normalize_failure(result.error_code, result.error or ""), FAILURE_AUTHENTICATION)

    def test_http_429_maps_to_rate_limited(self):
        adapter = _fake_adapter()
        exc = urllib.error.HTTPError("https://x", 429, "Too Many Requests", {},
                                     __import__("io").BytesIO(b"tokens per minute limit"))
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen", side_effect=exc):
            result = adapter.run(RunRequest("hello", model="groq/llama-3.3-70b-versatile"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "429")
        self.assertEqual(normalize_failure(result.error_code, result.error or ""), FAILURE_RATE_LIMITED)

    def test_socket_timeout_maps_to_timeout(self):
        adapter = _fake_adapter()
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen", side_effect=socket.timeout()):
            result = adapter.run(RunRequest("hello", model="groq/llama-3.3-70b-versatile"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "timeout")

    def test_network_error_maps_to_network_failure(self):
        adapter = _fake_adapter()
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("boom")):
            result = adapter.run(RunRequest("hello", model="groq/llama-3.3-70b-versatile"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "network_failure")

    def test_garbage_response_is_structured_failure(self):
        adapter = _fake_adapter()
        with mock.patch("harness2.adapters.direct.urllib.request.urlopen",
                        return_value=FakeResponse(b"{not json")):
            result = adapter.run(RunRequest("hello", model="groq/llama-3.3-70b-versatile"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "unknown_provider_failure")


class KernelDriverModelTests(unittest.TestCase):
    """Regression: the kernel driver must retain the decision model even when
    the typed constraints projection drops it (observed on the phone with the
    direct engine: decision.model set, adapter received None)."""

    class Capture(EngineAdapter):
        name = "direct"
        seen = None

        def status(self):
            return EngineStatus(
                "direct", True, True, True, CapabilityStatus.ACTIVE,
                "", ("reason.general",), "external", "free",
            )

        def run(self, request: RunRequest) -> EngineResult:
            self.seen = request.model
            return EngineResult("direct", True, text="ok")

    def test_model_survives_constraint_projection(self):
        from dataclasses import replace

        from harness2.circuit import CircuitBreaker
        from harness2.kernel.contracts import ExecutionPlan
        from harness2.store import Store

        adapter = self.Capture()
        request = RunRequest("hi", engine="direct", model="groq/llama-3.3-70b-versatile")
        routed = replace(request, model="groq/llama-3.3-70b-versatile")
        typed = execution_request_from_legacy(request)
        # Simulate an older/other kernel projection that drops the model.
        typed = replace(typed, constraints={**typed.constraints, "model": None})
        store = Store(os.path.join(tempfile.mkdtemp(), "state", "harness.db"))
        breaker = CircuitBreaker(store)
        driver = GovernedLegacyRuntimeDriver(
            adapter, routed, breaker, "direct:groq:groq/llama-3.3-70b-versatile",
        )
        plan = ExecutionPlan(
            plan_id="p", task_id="t", runtime_id="direct", capabilities=(),
            steps=("invoke.runtime",), verification=("validate.typed_outcome",),
            reason="test",
        )
        outcome = driver.execute(typed, plan)
        self.assertTrue(outcome.success)
        self.assertEqual(adapter.seen, "groq/llama-3.3-70b-versatile")


if __name__ == "__main__":
    unittest.main()
