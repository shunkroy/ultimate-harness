"""Engine adapter and orchestrator integration tests with mocked processes."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness2.adapters.opencode import OpenCodeAdapter, ZenAdapter
from harness2.adapters.prime import PrimeAdapter
from harness2.adapters.hermes import HermesAdapter
from harness2.config import HarnessConfig
from harness2.models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from harness2.orchestrator import Orchestrator
from harness2.security import atomic_write_json
from harness2.store import Store


def proc(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(["x"], code, stdout, stderr)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.config = HarnessConfig(state_root=os.path.join(self.tmp.name, "state"))
        self.config.ensure()

    def test_opencode_success_real_schema(self):
        stream = "\n".join((
            '{"type":"text","sessionID":"s","part":{"type":"text","text":"hello "}}',
            '{"type":"text","sessionID":"s","part":{"type":"text","text":"world"}}',
            '{"type":"step_finish","sessionID":"s","part":{"type":"step-finish"}}',
        ))
        with patch("subprocess.run", return_value=proc(stream)) as run:
            result = OpenCodeAdapter(self.config).run(RunRequest("private prompt", timeout=5))
        self.assertTrue(result.success)
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.session_id, "s")
        argv = run.call_args.args[0]
        self.assertIn("--format", argv)
        self.assertNotIn("private prompt", argv)
        prompt_path = argv[argv.index("--file") + 1]
        self.assertFalse(os.path.exists(prompt_path))

    def test_opencode_partial_then_error_fails(self):
        stream = "\n".join((
            '{"type":"text","part":{"type":"text","text":"partial"}}',
            '{"type":"error","error":{"name":"APIError","data":{"message":"failed"}}}',
        ))
        with patch("subprocess.run", return_value=proc(stream)):
            result = OpenCodeAdapter(self.config).run(RunRequest("x"))
        self.assertFalse(result.success)
        self.assertEqual(result.error, "failed")
        self.assertEqual(result.text, "partial")

    def test_zen_requires_key_and_namespace(self):
        adapter = ZenAdapter(self.config)
        self.assertFalse(adapter.run(RunRequest("x")).success)
        atomic_write_json(self.config.secrets_path, {"OPENCODE_API_KEY": "x" * 20})
        result = adapter.run(RunRequest("x", model="openai/gpt"))
        self.assertEqual(result.error_code, "invalid_model")

    def test_prime_nested_provider_error_fails(self):
        stream = "\n".join((
            '{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"no credits"}}',
            '{"type":"agent_end","messages":[]}',
        ))
        adapter = PrimeAdapter(self.config)
        with patch.object(adapter, "daemon_status") as ds, patch("subprocess.run", return_value=proc(stream)) as run:
            ds.return_value.healthy = True
            result = adapter.run(RunRequest("x"))
        self.assertFalse(result.success)
        self.assertEqual(result.error, "no credits")
        argv = run.call_args.args[0]
        self.assertNotIn("x", argv)
        attached = next(item[1:] for item in argv if item.startswith("@"))
        self.assertFalse(os.path.exists(attached))

    def test_clean_env_scopes_keys(self):
        atomic_write_json(self.config.secrets_path, {
            "OPENAI_API_KEY": "openai-secret", "OPENCODE_API_KEY": "zen-secret",
            "GEMINI_API_KEY": "gemini-secret",
        })
        env = self.config.clean_env("opencode", model="opencode/model")
        self.assertIn("OPENCODE_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertFalse(any(name.endswith("API_KEY") for name in self.config.clean_env("hermes")))

    def test_hermes_is_disabled_until_authorized_provider_is_explicitly_enabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_HERMES_ENABLED", None)
            status = HermesAdapter(self.config).status()
        self.assertTrue(status.available)
        self.assertFalse(status.enabled)
        self.assertFalse(status.healthy)


class FakeEngine:
    def __init__(self, name, results):
        self.name, self.results = name, list(results)

    def status(self):
        return EngineStatus(self.name, True, True, True, CapabilityStatus.ACTIVE)

    def run(self, request):
        return self.results.pop(0)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "harness.db"))

    def test_fallback_on_failure(self):
        engines = {
            "prime": FakeEngine("prime", [EngineResult("prime", False, error="down", error_code="x", exit_code=1)]),
            "opencode": FakeEngine("opencode", [EngineResult("opencode", True, text="ok")]),
        }
        orch = Orchestrator(engines, self.store)
        decision = __import__("harness2.models", fromlist=["RoutingDecision"]).RoutingDecision(
            "prime", None, None, "durable", ("opencode",), "durable"
        )
        with patch.object(orch, "decide", return_value=decision):
            chosen, result, run_id = orch.run(RunRequest("private", engine="auto"))
        self.assertEqual(chosen.engine, "opencode")
        self.assertTrue(result.success)
        self.assertTrue(run_id)

    def test_explicit_engine_never_falls_back(self):
        engines = {"prime": FakeEngine("prime", [EngineResult("prime", False, error="down", exit_code=1)])}
        orch = Orchestrator(engines, self.store)
        decision = __import__("harness2.models", fromlist=["RoutingDecision"]).RoutingDecision(
            "prime", None, None, "explicit", ("opencode",), "explicit"
        )
        with patch.object(orch, "decide", return_value=decision):
            chosen, result, _ = orch.run(RunRequest("private", engine="prime"))
        self.assertEqual(chosen.engine, "prime")
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
