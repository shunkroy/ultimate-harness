"""Static safety and release-contract tests for the GitHub installer."""

from __future__ import annotations

import subprocess
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import ssl
import threading


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
RELEASE_DIR = Path("/root/harness2-release")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@contextmanager
def local_https_release(directory: Path):
    cert = Path("/tmp/opencode/harness-release-cert.pem")
    key = Path("/tmp/opencode/harness-release-key.pem")
    if not cert.is_file() or not key.is_file():
        raise unittest.SkipTest("local TLS test certificate unavailable")

    def handler(*args, **kwargs):
        return QuietHandler(*args, directory=str(directory), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
        self.assertRegex(text, r'PINNED_SHA256="[0-9a-f]{64}"')

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
                "case $url in *SHA256SUMS) printf '%064d  harness2-2.1.1-py3-none-any.whl\\n' 0 >\"$out\";; *) printf x >\"$out\";; esac\n",
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

    def test_fallback_launcher_works_through_public_bin_symlink(self):
        wheel = RELEASE_DIR / "harness2-2.1.1-py3-none-any.whl"
        checksums = RELEASE_DIR / "SHA256SUMS-2.1.1"
        if not wheel.is_file() or not checksums.is_file():
            self.skipTest("local v2.1.1 release artifacts unavailable")
        cert = Path("/tmp/opencode/harness-release-cert.pem")
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as release:
            release_path = Path(release)
            shutil.copy2(wheel, release_path / wheel.name)
            shutil.copy2(checksums, release_path / "SHA256SUMS")
            with local_https_release(release_path) as base_url:
                env = {
                    **os.environ,
                    "HOME": temp,
                    "HARNESS_INSTALL_ROOT": str(Path(temp) / "install"),
                    "HARNESS_BIN_DIR": str(Path(temp) / "bin"),
                    "HARNESS_RELEASE_BASE_URL": base_url,
                    "HARNESS_START_SERVICE": "0",
                    "CURL_CA_BUNDLE": str(cert),
                }
                proc = subprocess.run(["/bin/sh", str(INSTALLER)], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            public_launcher = Path(temp) / "bin" / "harness"
            version = subprocess.run([str(public_launcher), "version"], capture_output=True, text=True)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), "2.1.1")


if __name__ == "__main__":
    unittest.main()
