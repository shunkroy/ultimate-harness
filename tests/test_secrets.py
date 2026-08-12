"""Cross-platform secret storage behavior."""

import os
import tempfile
import unittest
from unittest.mock import patch

from harness2 import secrets
from harness2.config import HarnessConfig
from harness2.jobs import JobManager
from harness2.models import EngineResult, RoutingDecision, RunRequest
from harness2.platforms import detect_platform
from harness2.store import Store


class FakeOrchestrator:
    def run(self, request):
        return RoutingDecision("opencode", "kiteretsu", "m", "test"), EngineResult("opencode", True, text="ok"), "run"


class SecretTests(unittest.TestCase):
    def test_posix_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "secrets.json")
            secrets.save(path, {"OPENAI_API_KEY": "x" * 20})
            self.assertEqual(secrets.load(path)["OPENAI_API_KEY"], "x" * 20)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_windows_dpapi_path_roundtrip_mocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "secrets.dpapi")
            with patch("harness2.secrets.os.name", "nt"), patch(
                "harness2.secrets._dpapi", side_effect=lambda data, decrypt=False: data[::-1]
            ):
                secrets.save(path, {"OPENAI_API_KEY": "y" * 20}, windows=True)
                self.assertEqual(secrets.load(path, windows=True)["OPENAI_API_KEY"], "y" * 20)

    def test_windows_jobs_use_dpapi_not_openssl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = detect_platform(
                sys_platform="win32", os_name="nt",
                env={"USERPROFILE": "C:/U", "APPDATA": "C:/A", "LOCALAPPDATA": "C:/L", "PATH": ""},
                home="C:/U", release="10",
            )
            config = HarnessConfig(
                platform=p, state_root=os.path.join(tmp, "state"),
                opencode_bin="C:/x/opencode.cmd", prime_bin="C:/x/prime.cmd",
                hermes_bin="C:/x/hermes.cmd", node_bin="C:/x/node.exe",
                python_bin="C:/x/python.exe", openssl_bin=None, prime_repo="C:/Prime",
            )
            config.ensure()
            store = Store(config.database_path)
            manager = JobManager(config, store, FakeOrchestrator())
            with patch("harness2.jobs.secret_store.protect_bytes", side_effect=lambda x: b"P" + x), patch(
                "harness2.jobs.secret_store.unprotect_bytes", side_effect=lambda x: x[1:]
            ):
                job = manager.submit(RunRequest("windows secret", engine="opencode"))
                self.assertEqual(manager.work_once()["status"], "succeeded")
                self.assertEqual(manager.show(job)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
