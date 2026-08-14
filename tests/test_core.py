"""Core policy, audit, security, circuit and CLI tests."""

from __future__ import annotations

import io
import argparse
import json
import os
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from unittest.mock import patch

from harness2.circuit import CircuitBreaker
from harness2.cli import build_parser, positive_int
from harness2.config import HarnessConfig
from harness2.doctor import _external_guardian_check, _integrity_checks, core_integrity_artifacts, integrity_artifacts
from harness2.models import CapabilityStatus, EngineResult, EngineStatus, RoutingDecision, RunRequest
from harness2.policy import PolicyRefusal, PolicyRouter
from harness2.security import atomic_write_json, ensure_private_dir, read_private_json, redact, task_hash
from harness2.store import Store


def statuses(local=False, prime=True):
    def item(name, available=True, healthy=True, enabled=True):
        return EngineStatus(name, available, healthy, enabled, CapabilityStatus.ACTIVE if healthy else CapabilityStatus.IMPLEMENTED)
    return {
        "opencode": item("opencode"), "zen": item("zen", True, False, False),
        "prime": item("prime", True, prime, True), "hermes": item("hermes"),
        "local": item("local", True, local, local),
    }


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_private_directory_mode(self):
        path = ensure_private_dir(os.path.join(self.tmp.name, "state"))
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)

    def test_atomic_private_json_and_merge_read(self):
        path = os.path.join(self.tmp.name, "state", "secrets.json")
        atomic_write_json(path, {"A": "x"})
        self.assertEqual(read_private_json(path), {"A": "x"})
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

    def test_atomic_json_rejects_destination_symlink(self):
        parent = ensure_private_dir(os.path.join(self.tmp.name, "state"))
        target = os.path.join(parent, "target")
        open(target, "w").close()
        link = os.path.join(parent, "secrets.json")
        os.symlink(target, link)
        with self.assertRaises(Exception):
            atomic_write_json(link, {"A": "x"})

    def test_redaction_and_hash(self):
        value = redact("api_key=abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", value)
        self.assertEqual(len(task_hash("hello")), 64)


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        prime_repo = os.path.join(self.tmp.name, "prime-repo")
        prime_bundle = os.path.join(prime_repo, "packages", "coding-agent", "dist", "bundle", "cli.js")
        os.makedirs(os.path.dirname(prime_bundle))
        with open(prime_bundle, "wb") as fh:
            fh.write(b"// deterministic Prime bundle fixture\n")
        self.config = HarnessConfig(
            state_root=os.path.join(self.tmp.name, "state"),
            prime_repo=prime_repo,
        )
        self.config.ensure()

    def test_missing_integrity_manifest_fails_closed(self):
        checks = _integrity_checks(self.config)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].name, "integrity.manifest")
        self.assertFalse(checks[0].ok)

    def test_integrity_manifest_detects_mismatch(self):
        artifacts = {**core_integrity_artifacts(self.config), **integrity_artifacts(self.config)}
        self.assertEqual(artifacts["harness.prime_wrapper"], self.config.prime_wrapper)
        self.assertEqual(artifacts["prime.bundle"], self.config.prime_bundle)
        expected = {}
        for name, path in artifacts.items():
            self.assertTrue(os.path.isfile(path), f"fixture artifact unavailable: {name}")
            with open(path, "rb") as fh:
                expected[name] = __import__("hashlib").sha256(fh.read()).hexdigest()
        expected["harness.module"] = "0" * 64
        atomic_write_json(self.config.integrity_manifest, {"schema": 1, "algorithm": "sha256", "artifacts": expected})
        checks = _integrity_checks(self.config)
        by_name = {item.name: item for item in checks}
        self.assertFalse(by_name["harness.module"].ok)
        self.assertTrue(by_name["harness.prime_wrapper"].ok)
        self.assertTrue(by_name["prime.bundle"].ok)
        self.assertTrue(by_name["integrity.manifest"].ok)


class DoctorTests(unittest.TestCase):
    def test_guardian_uses_last_scan_not_file_mtime(self):
        payload = {"last_scan": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 200))}
        mocked_open = unittest.mock.mock_open(read_data=json.dumps(payload))
        proc = unittest.mock.Mock(stdout="/bin/bash guardian_loop.sh\n")
        with patch("builtins.open", mocked_open), patch("harness2.doctor.subprocess.run", return_value=proc):
            self.assertTrue(_external_guardian_check("/state.json", "guardian_loop").ok)

    def test_guardian_rejects_stale_last_scan(self):
        payload = {"last_scan": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 301))}
        mocked_open = unittest.mock.mock_open(read_data=json.dumps(payload))
        proc = unittest.mock.Mock(stdout="python external_guardian.py\n")
        with patch("builtins.open", mocked_open), patch("harness2.doctor.subprocess.run", return_value=proc):
            self.assertFalse(_external_guardian_check("/state.json", "guardian").ok)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "state", "harness.db"))

    def test_audit_chain(self):
        self.store.append_audit("one", "x", {"a": "b"})
        self.store.append_audit("two", "y", {"c": "d"})
        self.assertEqual(self.store.verify_audit(), (True, 2, None))

    def test_audit_detects_tamper(self):
        self.store.append_audit("one", "x", {"a": "b"})
        with self.store.connect() as con:
            con.execute("UPDATE audit SET metadata_json='tampered' WHERE seq=1")
        self.assertFalse(self.store.verify_audit()[0])

    def test_run_record_stores_hash_not_prompt(self):
        request = RunRequest("private prompt content")
        decision = RoutingDecision("opencode", "kiteretsu", "free", "test")
        result = EngineResult("opencode", True, text="answer", duration=0.1)
        self.store.record_run(request, decision, result, time.time() - 0.1)
        with open(self.store.path, "rb") as fh:
            blob = fh.read()
        self.assertNotIn(b"private prompt content", blob)
        self.assertNotIn(b"answer", blob)

    def test_circuit_state(self):
        breaker = CircuitBreaker(self.store, threshold=2, base_cooldown=1)
        self.assertTrue(breaker.before("x").allowed)
        breaker.failure("x", "bad")
        self.assertTrue(breaker.before("x").allowed)
        breaker.failure("x", "bad again")
        self.assertFalse(breaker.before("x").allowed)
        value = self.store.circuit("x")
        value["opened_at"] = time.time() - value["cooldown"] - 1
        self.store.save_circuit(value)
        self.assertEqual(breaker.before("x").state, "half_open")
        breaker.success("x")
        self.assertEqual(breaker.before("x").state, "closed")


class PolicyTests(unittest.TestCase):
    def test_explicit_engine_wins(self):
        with patch("harness2.policy.expert_for_task", return_value=("inventor", "free", "x")):
            decision = PolicyRouter(statuses()).decide(RunRequest("code", engine="opencode"))
        self.assertEqual(decision.engine, "opencode")
        self.assertEqual(decision.agent, "inventor")

    def test_sensitive_requires_local(self):
        with self.assertRaises(PolicyRefusal):
            PolicyRouter(statuses(local=False)).decide(RunRequest("secret", sensitive=True))
        decision = PolicyRouter(statuses(local=True)).decide(RunRequest("secret", sensitive=True))
        self.assertEqual(decision.engine, "local")

    def test_untrusted_readonly_agent(self):
        decision = PolicyRouter(statuses()).decide(RunRequest("web content", untrusted=True))
        self.assertEqual((decision.engine, decision.agent), ("opencode", "harness-sandbox"))

    def test_durable_prefers_prime(self):
        with patch("harness2.policy.expert_for_task", return_value=("prime", None, "x")):
            decision = PolicyRouter(statuses()).decide(RunRequest("long-running IPython kernel"))
        self.assertEqual(decision.engine, "prime")
        self.assertIn("opencode", decision.fallbacks)

    def test_messaging_requires_hermes(self):
        decision = PolicyRouter(statuses()).decide(RunRequest("send telegram message"))
        self.assertEqual(decision.engine, "hermes")

    def test_guarded_cwd_refused(self):
        with tempfile.TemporaryDirectory() as root:
            project = os.path.join(root, "project")
            os.mkdir(project)
            with patch.dict(os.environ, {"HARNESS_GUARDED_ROOTS": root}):
                with self.assertRaisesRegex(PolicyRefusal, "guarded root"):
                    PolicyRouter(statuses()).decide(RunRequest("work", cwd=project))

    def test_guarded_roots_are_opt_in(self):
        with tempfile.TemporaryDirectory() as cwd, patch.dict(os.environ, {}, clear=True):
            decision = PolicyRouter(statuses()).decide(RunRequest("work", cwd=cwd))
            self.assertEqual(decision.engine, "opencode")

    def test_public_request_cannot_inject_prepared_cwd_identity(self):
        with tempfile.TemporaryDirectory() as root:
            identity = (os.stat(root).st_dev, os.stat(root).st_ino)
            with self.assertRaises(TypeError):
                RunRequest("work", cwd=root, cwd_identity=identity)  # type: ignore[call-arg]

    def test_sensitive_routing_never_invokes_external_classifier(self):
        with patch("harness2.policy.expert_for_task") as classifier:
            with self.assertRaises(PolicyRefusal):
                PolicyRouter(statuses(local=False)).decide(RunRequest("secret", sensitive=True))
        classifier.assert_not_called()

    def test_external_python_router_is_not_loaded(self):
        with patch.dict(os.environ, {"HARNESS_ROUTER": "/tmp/untrusted-router.py"}), patch(
            "builtins.__import__", wraps=__import__,
        ) as importer:
            decision = PolicyRouter(statuses()).decide(RunRequest("ordinary task"))
        self.assertEqual(decision.engine, "opencode")
        imported = [str(call.args[0]) for call in importer.call_args_list if call.args]
        self.assertNotIn("harness2_genius_router", imported)


class CliTests(unittest.TestCase):
    def test_timeout_validation(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            positive_int("0")
        self.assertEqual(positive_int("4"), 4)

    def test_parser_compatibility(self):
        parser = build_parser()
        args = parser.parse_args(["run", "hello", "--engine", "opencode", "--timeout", "5"])
        self.assertEqual(args.prompt, "hello")
        self.assertEqual(args.timeout, 5)
        args = parser.parse_args(["prime", "agents"])
        self.assertEqual(args.action, "agents")
        args = parser.parse_args(["integrity", "verify"])
        self.assertEqual(args.action, "verify")
        args = parser.parse_args(["task", "list", "--state", "ready"])
        self.assertEqual((args.command, args.action, args.state), ("task", "list", "ready"))
        args = parser.parse_args(["events", "replay", "--after", "4"])
        self.assertEqual((args.command, args.action, args.after), ("events", "replay", 4))
        args = parser.parse_args(["provider", "scores", "--capability", "code.execute"])
        self.assertEqual((args.command, args.action), ("provider", "scores"))
        args = parser.parse_args(["resources", "status"])
        self.assertEqual((args.command, args.action), ("resources", "status"))

    def test_context_submit_cli_uses_runtime_factory(self):
        from harness2.cli import main
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            source = os.path.join(tmp, "source.txt")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("# Concepts\nOne: Two\n")
            state = os.path.join(tmp, "state")
            config = HarnessConfig(state_root=state)
            config.ensure()
            store = Store(config.database_path)
            app = unittest.mock.Mock()
            app.config, app.store = config, store
            app.context_jobs.return_value = unittest.mock.Mock()
            app.context_jobs.return_value.submit.return_value = "a" * 32
            output = io.StringIO()
            with patch("harness2.cli.bootstrap", return_value=app), redirect_stdout(output):
                code = main(["context", "submit", source, "--name", "CLI Context"])
            self.assertEqual(code, 0)
            app.context_jobs.return_value.submit.assert_called_once_with(
                source, name="CLI Context", version="0.1.0",
            )

    def test_service_child_preserves_explicit_state_root(self):
        from harness2.cli import cmd_svc
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = HarnessConfig(state_root=os.path.join(tmp, "custom-state"))
            config.ensure()
            store = Store(config.database_path)
            engines = {}
            orchestrator = unittest.mock.Mock()
            process = unittest.mock.Mock(pid=4321)
            args = argparse.Namespace(action="up", json=False, interval=30)
            with patch("harness2.cli.runtime", return_value=(config, store, engines, orchestrator)), patch(
                "harness2.cli.service_status", return_value=(os.path.join(config.state_root, "run", "service.pid"), None, False),
            ), patch("harness2.cli.supervisor.acquire_start_lock") as lock, patch(
                "harness2.cli.subprocess.Popen", return_value=process,
            ) as popen, patch("harness2.cli.supervisor.write_pidfile"):
                lock.return_value.release.return_value = None
                code = cmd_svc(args)
            self.assertEqual(code, 0)
            self.assertEqual(popen.call_args.kwargs["env"]["HARNESS2_HOME"], config.state_root)


if __name__ == "__main__":
    unittest.main()
