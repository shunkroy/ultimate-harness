"""Static safety and release-contract tests for the GitHub installer."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
STABLE_VERSION = "2.1.1"
STABLE_RELEASE_SHA256 = "dc04ba18b166ba7783bcfc754cc0db4ca28cc79db919963fbc09268d21b84955"
POSIX_INSTALLER = os.name == "posix" and Path("/bin/sh").is_file()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@contextmanager
def local_https_release(directory: Path, cert: Path, key: Path):
    def handler(*args, **kwargs):
        return QuietHandler(*args, directory=str(directory), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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


def generate_tls_material(directory: Path) -> tuple[Path, Path]:
    """Generate a loopback-only certificate inside the test sandbox."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("OpenSSL is required to provision the local HTTPS test fixture")
    directory.mkdir(parents=True)
    cert = directory / "localhost-cert.pem"
    key = directory / "localhost-key.pem"
    proc = subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-days", "2", "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
            "-addext", "extendedKeyUsage=serverAuth",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to generate local TLS fixture: {proc.stderr.strip()}")
    key.chmod(0o600)
    return cert, key


def _record_digest(content: bytes) -> str:
    value = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")
    return f"sha256={value}"


def create_stable_wheel_fixture(directory: Path) -> tuple[Path, str]:
    """Build a deterministic minimal v2.1.1 wheel for installer control-flow tests."""
    directory.mkdir(parents=True)
    wheel = directory / f"harness2-{STABLE_VERSION}-py3-none-any.whl"
    dist_info = f"harness2-{STABLE_VERSION}.dist-info"
    payloads = {
        "harness2/__init__.py": f'__version__ = "{STABLE_VERSION}"\n'.encode(),
        "harness2/cli.py": (
            "from __future__ import annotations\n"
            "import sys\n\n"
            "def main(argv=None):\n"
            "    args = list(sys.argv[1:] if argv is None else argv)\n"
            f"    if args == ['version']:\n        print('{STABLE_VERSION}')\n        return 0\n"
            "    if args == ['integrity', 'pin']:\n        return 0\n"
            "    print('unsupported fixture command', file=sys.stderr)\n"
            "    return 2\n\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        ).encode(),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: harness2\n"
            f"Version: {STABLE_VERSION}\n"
            "Requires-Python: >=3.11\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: harness2-installer-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n\n"
        ).encode(),
        f"{dist_info}/entry_points.txt": b"[console_scripts]\nharness = harness2.cli:main\n",
    }
    record = "".join(
        f"{name},{_record_digest(content)},{len(content)}\n"
        for name, content in sorted(payloads.items())
    )
    record_name = f"{dist_info}/RECORD"
    payloads[record_name] = (record + f"{record_name},,\n").encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return wheel, digest


def create_fixture_installer(directory: Path, fixture_sha256: str) -> Path:
    """Copy the production installer while pinning only the generated test wheel."""
    text = INSTALLER.read_text(encoding="utf-8")
    marker = f'PINNED_SHA256="{STABLE_RELEASE_SHA256}"'
    if text.count(marker) != 1:
        raise AssertionError("production installer stable checksum pin changed unexpectedly")
    fixture = directory / "install-fixture.sh"
    fixture.write_text(text.replace(marker, f'PINNED_SHA256="{fixture_sha256}"'), encoding="utf-8")
    fixture.chmod(0o700)
    return fixture


def create_controlled_tools(directory: Path, invocation_log: Path) -> Path:
    """Create a PATH whose python3 fails only the venv creation command."""
    directory.mkdir(parents=True)
    real_python = str(Path(sys.executable).resolve())
    wrapper = directory / "python3"
    wrapper.write_text(
        f"#!{real_python}\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        f"with open({str(invocation_log)!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:], separators=(',', ':')) + '\\n')\n"
        "if sys.argv[1:3] == ['-m', 'venv']:\n"
        "    raise SystemExit(73)\n"
        f"os.execv({real_python!r}, [{real_python!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    for command in ("cat", "chmod", "curl", "cut", "ln", "mkdir", "mktemp", "sed", "sha256sum"):
        target = shutil.which(command)
        if not target:
            raise RuntimeError(f"required installer test command is unavailable: {command}")
        (directory / command).symlink_to(Path(target))
    return directory


def installer_environment(root: Path, base_url: str, cert: Path, tools: Path) -> dict[str, str]:
    home = root / "home"
    temp = root / "tmp"
    for directory in (home, temp, root / "state", root / "config", root / "cache"):
        directory.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": str(tools),
        "TMPDIR": str(temp),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "HARNESS2_HOME": str(root / "state" / "harness2"),
        "HARNESS_INSTALL_ROOT": str(root / "install"),
        "HARNESS_BIN_DIR": str(root / "bin"),
        "HARNESS_RELEASE_BASE_URL": base_url,
        "HARNESS_START_SERVICE": "0",
        "CURL_CA_BUNDLE": str(cert),
        "NO_PROXY": "127.0.0.1,localhost",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONNOUSERSITE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


@contextmanager
def local_installer_fixture(root: Path, *, manifest_sha256: str | None = None, tamper_wheel: bool = False):
    release = root / "release"
    wheel, fixture_sha256 = create_stable_wheel_fixture(release)
    (release / "SHA256SUMS").write_text(
        f"{manifest_sha256 or fixture_sha256}  {wheel.name}\n",
        encoding="utf-8",
    )
    if tamper_wheel:
        with wheel.open("ab") as fh:
            fh.write(b"tampered")
    installer = create_fixture_installer(root, fixture_sha256)
    cert, key = generate_tls_material(root / "tls")
    invocation_log = root / "python-invocations.jsonl"
    tools = create_controlled_tools(root / "tools", invocation_log)
    with local_https_release(release, cert, key) as base_url:
        yield installer, installer_environment(root, base_url, cert, tools), invocation_log


def read_invocations(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class InstallerTests(unittest.TestCase):
    @unittest.skipUnless(POSIX_INSTALLER, "POSIX installer contract")
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
        self.assertIn(f'VERSION="{STABLE_VERSION}"', text)
        self.assertIn(f'PINNED_SHA256="{STABLE_RELEASE_SHA256}"', text)

    def test_installer_uses_isolated_venv_and_bounded_service(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('$HOME/.local/share/harness2', text)
        self.assertIn('--target "$VENV/site-packages"', text)
        self.assertIn("PYTHONPATH='$VENV/site-packages'", text)
        self.assertIn("PYTHONSAFEPATH=1", text)
        self.assertIn('svc restart --interval', text)
        self.assertIn('HARNESS_START_SERVICE', text)

    def test_installer_contains_no_credential_names_or_values(self):
        text = INSTALLER.read_text(encoding="utf-8").lower()
        for token in ("openai_api_key", "github_pat_", "password=", "authorization:"):
            self.assertNotIn(token, text)

    @unittest.skipUnless(POSIX_INSTALLER, "POSIX installer contract")
    def test_installer_rejects_a_checksum_not_matching_its_pin(self):
        with tempfile.TemporaryDirectory() as temp, local_installer_fixture(
            Path(temp), manifest_sha256="0" * 64,
        ) as (installer, env, invocation_log):
            proc = subprocess.run(
                ["/bin/sh", str(installer)], env=env,
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not match the installer pin", proc.stderr)
            self.assertFalse((Path(temp) / "bin" / "harness").exists())
            self.assertFalse(any(args[:2] == ["-m", "pip"] for args in read_invocations(invocation_log)))

    @unittest.skipUnless(POSIX_INSTALLER, "POSIX installer contract")
    def test_installer_rejects_a_wheel_not_matching_the_manifest(self):
        with tempfile.TemporaryDirectory() as temp, local_installer_fixture(
            Path(temp), tamper_wheel=True,
        ) as (installer, env, invocation_log):
            proc = subprocess.run(
                ["/bin/sh", str(installer)], env=env,
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("wheel checksum mismatch", proc.stderr)
            self.assertFalse((Path(temp) / "bin" / "harness").exists())
            self.assertFalse(any(args[:2] == ["-m", "pip"] for args in read_invocations(invocation_log)))

    @unittest.skipUnless(POSIX_INSTALLER, "POSIX installer contract")
    def test_fallback_launcher_works_through_public_bin_symlink(self):
        with tempfile.TemporaryDirectory() as temp, local_installer_fixture(
            Path(temp),
        ) as (installer, env, invocation_log):
            proc = subprocess.run(
                ["/bin/sh", str(installer)], env=env,
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            public_launcher = Path(temp) / "bin" / "harness"
            fallback_target = Path(temp) / "install" / "venv" / "bin" / "harness"
            self.assertTrue(public_launcher.is_symlink())
            self.assertEqual(os.readlink(public_launcher), str(fallback_target))
            self.assertTrue((Path(temp) / "install" / "venv" / "site-packages" / "harness2" / "cli.py").is_file())
            self.assertFalse((Path(temp) / "install" / "venv" / "bin" / "python").exists())
            version = subprocess.run(
                [str(public_launcher), "version"], env=env,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), STABLE_VERSION)
            invocations = read_invocations(invocation_log)
            self.assertIn(["-m", "venv", str(Path(temp) / "install" / "venv")], invocations)
            self.assertTrue(any(args[:2] == ["-m", "pip"] and "--target" in args for args in invocations))
            self.assertIn(["-P", "-m", "harness2.cli", "integrity", "pin"], invocations)


if __name__ == "__main__":
    unittest.main()
