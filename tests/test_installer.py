"""Static safety and release-contract tests for the GitHub installer."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-release.sh"


class InstallerTests(unittest.TestCase):
    def test_installer_is_posix_shell_valid(self):
        proc = subprocess.run(["sh", "-n", str(INSTALLER)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_installer_verifies_release_before_install(self):
        text = INSTALLER.read_text(encoding="utf-8")
        checksum = text.index('ACTUAL="$(sha256sum')
        install = text.index('pip install --no-deps')
        self.assertLess(checksum, install)
        self.assertIn("--proto '=https'", text)
        self.assertIn("--tlsv1.2", text)

    def test_installer_uses_isolated_venv_and_bounded_service(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('$HOME/.local/share/harness2', text)
        self.assertIn('svc restart --interval', text)
        self.assertIn('HARNESS_START_SERVICE', text)

    def test_installer_contains_no_credential_names_or_values(self):
        text = INSTALLER.read_text(encoding="utf-8").lower()
        for token in ("openai_api_key", "github_pat_", "password=", "authorization:"):
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
