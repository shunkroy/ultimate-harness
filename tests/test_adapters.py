"""Engine adapter and orchestrator integration tests with mocked processes."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest.mock import patch

from harness2.adapters.opencode import OpenCodeAdapter, ZenAdapter
from harness2.adapters.prime import PrimeAdapter
from harness2.adapters.hermes import HermesAdapter
from harness2.adapters.local import LocalAdapter
from harness2.config import HarnessConfig
from harness2.models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from harness2.execution import ProcessResult
from harness2.orchestrator import Orchestrator
from harness2.policy import canonicalize_request
from harness2.security import atomic_write_json
from harness2.store import Store


def proc(stdout="", stderr="", code=0):
    return ProcessResult(code, stdout, stderr, 0.01, False, False, "a" * 64)


def fake_executable(directory: str, name: str) -> str:
    """Create a provider command fixture without requiring a real CLI install."""
    suffix = ".cmd" if os.name == "nt" else ""
    path = os.path.join(directory, name + suffix)
    content = "@exit /b 0\r\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        fixture_bin = os.path.join(self.tmp.name, "bin")
        prime_repo = os.path.join(self.tmp.name, "prime-repo")
        os.makedirs(fixture_bin)
        os.makedirs(prime_repo)
        self.config = HarnessConfig(
            state_root=os.path.join(self.tmp.name, "state"),
            opencode_bin=fake_executable(fixture_bin, "opencode"),
            prime_bin=fake_executable(fixture_bin, "prime-agent"),
            hermes_bin=fake_executable(fixture_bin, "hermes"),
            prime_repo=prime_repo,
        )
        self.config.ensure()

    def test_missing_provider_binaries_report_unavailable(self):
        missing = os.path.join(self.tmp.name, "missing")
        config = HarnessConfig(
            state_root=os.path.join(self.tmp.name, "missing-state"),
            opencode_bin=os.path.join(missing, "opencode"),
            prime_bin=os.path.join(missing, "prime-agent"),
            hermes_bin=os.path.join(missing, "hermes"),
            prime_repo=os.path.join(self.tmp.name, "missing-prime-repo"),
        )
        for adapter in (OpenCodeAdapter(config), PrimeAdapter(config), HermesAdapter(config)):
            with self.subTest(provider=adapter.name):
                status = adapter.status()
                self.assertFalse(status.available)
                self.assertFalse(status.healthy)
        result = OpenCodeAdapter(config).run(RunRequest("private"))
        self.assertEqual(result.error_code, "unavailable")

    @unittest.skipIf(os.name == "nt", "POSIX executable-mode semantics")
    def test_non_executable_provider_is_unavailable_and_symlink_is_canonicalized(self):
        target = os.path.join(self.tmp.name, "real-provider")
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("#!/bin/sh\nexit 0\n")
        os.chmod(target, 0o600)
        unavailable = HarnessConfig(
            state_root=os.path.join(self.tmp.name, "nonexec-state"),
            opencode_bin=target,
        )
        self.assertFalse(OpenCodeAdapter(unavailable).status().available)

        os.chmod(target, 0o700)
        link = os.path.join(self.tmp.name, "provider-link")
        os.symlink(target, link)
        canonical = HarnessConfig(
            state_root=os.path.join(self.tmp.name, "link-state"),
            opencode_bin=link,
        )
        self.assertEqual(canonical.opencode_bin, os.path.realpath(target))
        os.unlink(link)
        os.symlink(os.path.join(self.tmp.name, "missing"), link)
        self.assertTrue(canonical.executable_available("opencode"))

    def test_opencode_success_real_schema(self):
        stream = "\n".join((
            '{"type":"text","sessionID":"s","part":{"type":"text","text":"hello "}}',
            '{"type":"text","sessionID":"s","part":{"type":"text","text":"world"}}',
            '{"type":"step_finish","sessionID":"s","part":{"type":"step-finish"}}',
        ))
        with patch("harness2.adapters.opencode.run_process", return_value=proc(stream)) as run:
            result = OpenCodeAdapter(self.config).run(RunRequest("private prompt", timeout=5))
        self.assertTrue(result.success)
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.session_id, "s")
        argv = run.call_args.args[0].argv
        self.assertIn("--format", argv)
        self.assertNotIn("private prompt", argv)
        prompt_path = argv[argv.index("--file") + 1]
        self.assertFalse(os.path.exists(prompt_path))

    def test_opencode_partial_then_error_fails(self):
        stream = "\n".join((
            '{"type":"text","part":{"type":"text","text":"partial"}}',
            '{"type":"error","error":{"name":"APIError","data":{"message":"failed"}}}',
        ))
        with patch("harness2.adapters.opencode.run_process", return_value=proc(stream)):
            result = OpenCodeAdapter(self.config).run(RunRequest("x"))
        self.assertFalse(result.success)
        self.assertEqual(result.error, "failed")
        self.assertEqual(result.text, "partial")

    def test_opencode_output_limit_fails_closed(self):
        limited = ProcessResult(0, "partial", "", 0.01, False, True, "b" * 64)
        with patch("harness2.adapters.opencode.run_process", return_value=limited):
            result = OpenCodeAdapter(self.config).run(RunRequest("x"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "output_limit")
        self.assertEqual(result.exit_code, 125)

    def test_opencode_omits_unconfigured_agent_and_model(self):
        stream = '{"type":"step_finish","part":{"type":"step-finish"}}'
        with patch("harness2.adapters.opencode.run_process", return_value=proc(stream)) as run:
            result = OpenCodeAdapter(self.config).run(RunRequest("x"))
        self.assertTrue(result.success)
        argv = run.call_args.args[0].argv
        self.assertNotIn("--agent", argv)
        self.assertNotIn("-m", argv)

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
        with patch.object(adapter, "daemon_status") as ds, patch(
            "harness2.adapters.prime.run_process", return_value=proc(stream),
        ) as run:
            ds.return_value.healthy = True
            result = adapter.run(RunRequest("x"))
        self.assertFalse(result.success)
        self.assertEqual(result.error, "no credits")
        argv = run.call_args.args[0].argv
        self.assertNotIn("x", argv)
        authority = run.call_args.args[0].additional_cwd_authorities
        self.assertEqual(authority[0][0], os.path.realpath(os.getcwd()))
        self.assertEqual(authority[0][1], (os.stat(os.getcwd()).st_dev, os.stat(os.getcwd()).st_ino))
        attached = next(item[1:] for item in argv if item.startswith("@"))
        self.assertFalse(os.path.exists(attached))

    @unittest.skipIf(os.name == "nt", "symlink retarget fixture requires POSIX semantics")
    def test_prime_rejects_requested_cwd_retarget_before_process_invocation(self):
        first = os.path.join(self.tmp.name, "prime-workspace-first")
        second = os.path.join(self.tmp.name, "prime-workspace-second")
        alias = os.path.join(self.tmp.name, "prime-workspace")
        os.mkdir(first)
        os.mkdir(second)
        os.symlink(first, alias)
        prepared = canonicalize_request(RunRequest("x", cwd=alias))
        os.unlink(alias)
        moved = first + "-moved"
        os.rename(first, moved)
        os.symlink(second, first)
        adapter = PrimeAdapter(self.config)
        with patch("harness2.adapters.prime.run_process") as run:
            result = adapter.run(prepared)
        self.assertEqual(result.error_code, "invalid_execution")
        run.assert_not_called()

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
        with patch.object(HarnessConfig, "secrets", side_effect=RuntimeError("foreign secret store")):
            self.assertIn("PATH", self.config.clean_env("local"))
            self.assertIn("PATH", self.config.clean_env("hermes"))

    def test_hermes_is_disabled_until_authorized_provider_is_explicitly_enabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_HERMES_ENABLED", None)
            status = HermesAdapter(self.config).status()
        self.assertTrue(status.available)
        self.assertFalse(status.enabled)
        self.assertFalse(status.healthy)
        with patch("harness2.adapters.hermes.run_process") as run:
            result = HermesAdapter(self.config).run(RunRequest("private"))
        self.assertEqual(result.error_code, "disabled")
        run.assert_not_called()

    def test_local_http_body_is_bounded(self):
        config = HarnessConfig(
            state_root=os.path.join(self.tmp.name, "local-state"),
            http_body_limit=1024,
        )
        config.ensure()
        store = Store(config.database_path)
        adapter = LocalAdapter(config, store)
        adapter.set_enabled(True)
        response = io.BytesIO(b"x" * 1025)
        response.geturl = lambda: str(config.local_url)  # type: ignore[attr-defined]
        with patch("harness2.adapters.local._open", return_value=response):
            result = adapter.run(RunRequest("private"))
        self.assertEqual(result.error_code, "output_limit")
        self.assertEqual(result.exit_code, 125)

    def test_local_http_total_deadline_and_redirect_are_fail_closed(self):
        config = HarnessConfig(
            state_root=os.path.join(self.tmp.name, "local-deadline-state"),
            http_body_limit=1024,
        )
        config.ensure()
        store = Store(config.database_path)
        adapter = LocalAdapter(config, store)
        adapter.set_enabled(True)

        class SlowBody(io.BytesIO):
            def read(self, size=-1):
                __import__("time").sleep(0.06)
                return super().read(size)

            def geturl(self):
                return str(config.local_url)

        with patch("harness2.adapters.local._open", return_value=SlowBody(b"{}")):
            timed = adapter.run(RunRequest("private", timeout=0.05))
        self.assertEqual(timed.error_code, "timeout")

        redirected = io.BytesIO(b"{}")
        redirected.geturl = lambda: "https://example.invalid/response"  # type: ignore[attr-defined]
        with patch("harness2.adapters.local._open", return_value=redirected):
            unsafe = adapter.run(RunRequest("private"))
        self.assertEqual(unsafe.error_code, "unsafe_endpoint")


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
