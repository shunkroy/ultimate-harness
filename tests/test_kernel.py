"""Provider-independent kernel, catalog and discovery fitness tests."""

from __future__ import annotations

import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.discovery import CliProbeSpec, probe_cli
from harness2.kernel.catalog import build_catalog
from harness2.kernel.contracts import (
    CapabilityDescriptor,
    CapabilityEvidence,
    EvidenceKind,
    Health,
    Maturity,
    RuntimeDescriptor,
)
from harness2.kernel.registry import CapabilityRegistry, RegistryConflict, RuntimeRegistry
from harness2.models import CapabilityStatus, EngineStatus


class RegistryTests(unittest.TestCase):
    def descriptor(self, name="provider-a", capabilities=("code.execute",)):
        return RuntimeDescriptor(
            name, "cli", name, "1.0.0", "/bin/true", "cli", "file", "json",
            capabilities, maturity=Maturity.TESTED, health=Health.HEALTHY,
        )

    def test_runtime_registry_is_provider_neutral(self):
        registry = RuntimeRegistry([self.descriptor()])
        self.assertEqual(registry.supporting("code.execute")[0].id, "provider-a")
        self.assertIsNone(registry.get("opencode"))

    def test_duplicate_registration_requires_explicit_replace(self):
        registry = RuntimeRegistry([self.descriptor()])
        with self.assertRaises(RegistryConflict):
            registry.register(self.descriptor())
        registry.register(self.descriptor(capabilities=("reason.general",)), replace=True)
        self.assertEqual(registry.get("provider-a").capabilities, ("reason.general",))

    def test_capability_validation_catches_missing_provider(self):
        capability = CapabilityDescriptor(
            "code.execute", "execute code", Maturity.IMPLEMENTED, ("missing",),
        )
        registry = CapabilityRegistry([capability])
        self.assertIn("provider not registered", registry.validate(RuntimeRegistry())[0])

    def test_catalog_survives_without_any_named_external_provider(self):
        runtimes, capabilities = build_catalog({})
        self.assertIsNotNone(runtimes.get("harness"))
        self.assertIsNotNone(capabilities.get("context.execute.query"))
        self.assertEqual(capabilities.validate(runtimes), ())

    def test_catalog_projects_legacy_status_without_claiming_benchmarks(self):
        statuses = {
            "opencode": EngineStatus(
                "opencode", True, True, True, CapabilityStatus.ACTIVE,
                "ready", (), "private-file",
            )
        }
        runtimes, capabilities = build_catalog(statuses)
        self.assertEqual(runtimes.get("opencode").health, Health.HEALTHY)
        self.assertNotEqual(capabilities.get("reason.general").maturity, Maturity.BENCHMARKED)


class ArchitectureFitnessTests(unittest.TestCase):
    def test_kernel_does_not_import_concrete_adapters(self):
        root = Path(__file__).resolve().parents[1] / "harness2" / "kernel"
        violations = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = getattr(node, "module", "") or ""
                if "adapters" in module:
                    violations.append(f"{path.name}:{getattr(node, 'lineno', 0)}")
        self.assertEqual(violations, [])


class DiscoveryTests(unittest.TestCase):
    def test_missing_cli_is_observed_not_test_verified(self):
        spec = CliProbeSpec(
            "future", "Future CLI", "/missing/future", ("--version",),
            "cli", "stdin", "json", ("reason.general",),
        )
        value = probe_cli(spec)
        self.assertEqual(value.health, Health.DOWN)
        self.assertFalse(value.enabled)
        self.assertEqual(value.evidence[0].kind, EvidenceKind.LOCAL_OBSERVATION)

    def test_version_probe_is_fixed_and_bounded(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            executable = os.path.join(tmp, "provider")
            with open(executable, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nprintf 'Provider 3.4.5\\n'\n")
            os.chmod(executable, 0o700)
            spec = CliProbeSpec(
                "provider", "Provider", executable, ("--version",),
                "cli", "file", "json", ("code.execute",), timeout=2,
            )
            with patch("harness2.discovery.subprocess.run", wraps=subprocess.run) as run:
                value = probe_cli(spec, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(value.version, "3.4.5")
            self.assertEqual(value.health, Health.HEALTHY)
            self.assertEqual(run.call_args.args[0], [executable, "--version"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2)


if __name__ == "__main__":
    unittest.main()
