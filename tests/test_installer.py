"""Static safety and release-contract tests for the GitHub installer."""

from __future__ import annotations

import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallerTests(unittest.TestCase):
    def test_installer_is_posix_shell_valid(self):
        proc = subprocess.run(["sh", "-n", str(INSTALLER)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_installer_verifies_release_before_install(self):
        text = INSTALLER.read_text(encoding="utf-8")
        checksum = text.index('ACTUAL="$(sha256sum')
        install = text.index('pip --disable-pip-version-check --no-input install')
        self.assertLess(checksum, install)
        self.assertIn("--proto '=https'", text)
        self.assertIn("--tlsv1.2", text)
        self.assertIn('PINNED_SHA256="d7fe4ae5d7eeb0a8b6f0dc13843934420c56f00956b9354fa14a2491eb25fcfe"', text)

    def test_installer_uses_isolated_venv_and_bounded_service(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('$HOME/.local/share/harness2', text)
        self.assertIn('--target "$VENV/site-packages"', text)
        self.assertIn("PYTHONPATH='$VENV/site-packages'", text)
        self.assertIn('svc restart --interval', text)
        self.assertIn('HARNESS_START_SERVICE', text)

    def test_installer_contains_no_credential_names_or_values(self):
        text = INSTALLER.read_text(encoding="utf-8").lower()
        for token in ("openai_api_key", "github_pat_", "password=", "authorization:"):
            self.assertNotIn(token, text)

    def test_installer_rejects_a_checksum_not_matching_its_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            fake_bin = Path(temp) / "bin"
            fake_bin.mkdir()
            for command in ("python3", "mktemp", "sed", "sha256sum"):
                target = shutil.which(command)
                self.assertIsNotNone(target)
                (fake_bin / command).symlink_to(Path(target))
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = --output ]; then out=$2; shift 2; continue; fi\n"
                "  url=$1; shift\n"
                "done\n"
                "case $url in *SHA256SUMS) printf '%064d  harness2-2.1.0-py3-none-any.whl\\n' 0 >\"$out\";; *) printf x >\"$out\";; esac\n",
                encoding="utf-8",
            )
            curl.chmod(0o755)
            proc = subprocess.run(
                ["/bin/sh", str(INSTALLER)],
                env={"HOME": temp, "PATH": str(fake_bin)},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not match the installer pin", proc.stderr)
            self.assertFalse((Path(temp) / ".local" / "bin" / "harness").exists())


if __name__ == "__main__":
    unittest.main()
