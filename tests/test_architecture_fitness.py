"""Architecture invariants that future implementation must not erode."""

from __future__ import annotations

import ast
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness2.config import HarnessConfig
from harness2.kernel.contracts import Maturity
from harness2.service import active_status


ROOT = Path(__file__).resolve().parents[1] / "harness2"


class ArchitectureFitnessTests(unittest.TestCase):
    def test_kernel_has_no_concrete_provider_or_legacy_imports(self):
        forbidden = ("adapters", "orchestrator", "policy", "opencode", "prime", "hermes", "local")
        violations = []
        for path in (ROOT / "kernel").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = getattr(node, "module", "") or ""
                parts = module.split(".")
                if any(part in parts for part in forbidden):
                    violations.append(f"{path.name}:{getattr(node, 'lineno', 0)}:{module}")
        self.assertEqual(violations, [])

    def test_cli_has_no_direct_store_or_adapter_construction(self):
        tree = ast.parse((ROOT / "cli.py").read_text(encoding="utf-8"))
        constructed = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"Store", "Orchestrator", "OpenCodeAdapter", "PrimeAdapter"}:
                    constructed.append((node.func.id, node.lineno))
        self.assertEqual(constructed, [])

    def test_runtime_driver_protocol_has_one_authoritative_definition(self):
        definitions = []
        for path in (ROOT / "kernel").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "RuntimeDriver":
                    definitions.append(f"{path.name}:{node.lineno}")
        self.assertEqual(len(definitions), 1)
        self.assertTrue(definitions[0].startswith("registry.py:"))

    def test_external_cli_adapters_use_the_bounded_execution_boundary(self):
        for name in ("opencode.py", "prime.py", "hermes.py"):
            text = (ROOT / "adapters" / name).read_text(encoding="utf-8")
            with self.subTest(adapter=name):
                self.assertIn("run_process", text)
                self.assertNotIn("subprocess.run", text)

    def test_maturity_order_does_not_equate_health_or_benchmarking(self):
        self.assertNotEqual(Maturity.TESTED, Maturity.BENCHMARKED)
        self.assertNotEqual(Maturity.IMPLEMENTED, Maturity.STABLE)

    def test_desired_always_active_cannot_masquerade_as_fresh_process(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            config = HarnessConfig(state_root=os.path.join(temp, "state"))
            config.ensure()
            with patch("harness2.service.read_private_json", return_value={
                "service_pid": 99, "heartbeat_at": time.time(), "observed_state": "active",
            }), patch("harness2.service.supervisor.read_pidfile", return_value=99), patch(
                "harness2.service.service_process_matches", return_value=False,
            ):
                value = active_status(config)
            self.assertTrue(value["desired_always_active"])
            self.assertFalse(value["active"])


if __name__ == "__main__":
    unittest.main()
