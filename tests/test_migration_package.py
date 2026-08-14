"""Offline migration package and restore acceptance tests."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from harness2.config import HarnessConfig
from harness2.context.jobs import ContextJobManager
from harness2.context.package import ContextPackage
from harness2.kernel.event_bus import EventBus
from harness2.kernel.execution_state import ExecutionStateRepository
from harness2.kernel.task_types import default_task_types
from harness2.kernel.tasks import TaskRepository
from harness2.storage import LocalAuthenticatedStorage
from harness2.store import Store


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migration" / "build_package.py"
RESTORE = ROOT / "scripts" / "migration" / "restore_state.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_module("migration_builder", SCRIPT)
restorer = load_module("migration_restorer", RESTORE)


@unittest.skipUnless(shutil.which("git") and shutil.which("openssl"), "git and OpenSSL required")
class MigrationPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.out = self.root / "out" / "migration"
        self.out.parent.mkdir()
        self.repo.mkdir()
        self.state.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.repo, check=True)
        (self.repo / "app.py").write_text("print('fixture')\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.repo, check=True)
        self.sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout.strip()

        self.secret_value = "TOP-SECRET-VALUE-9eab"
        (self.state / "secrets.json").write_text(
            json.dumps({"OPENAI_API_KEY": self.secret_value}), encoding="utf-8",
        )
        (self.state / "job.key").write_text("JOB-KEY-SECRET", encoding="utf-8")
        (self.state / "object-store.key").write_text("OBJECT-KEY-SECRET", encoding="utf-8")
        (self.state / "integrity.json").write_text('{"schema":"fixture"}\n', encoding="utf-8")
        for name in ("jobs", "contexts", "context-jobs", "objects", "run", "tmp", "logs", "caches"):
            (self.state / name).mkdir()
        (self.state / "jobs" / "abc.bin").write_bytes(b"encrypted-job-payload")
        (self.state / "run" / "discard.pid").write_text("123", encoding="ascii")
        (self.state / "logs" / "service.log").write_text(self.secret_value, encoding="utf-8")
        self.cwd = self.root / "usable-cwd"
        self.cwd.mkdir()
        with closing(sqlite3.connect(self.state / "harness.db")) as con:
            con.execute("CREATE TABLE marker(value TEXT)")
            con.execute("INSERT INTO marker VALUES ('snapshot-value')")
            con.execute("CREATE TABLE jobs(id TEXT,status TEXT,payload_path TEXT,cwd TEXT)")
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?)",
                ("abc", "succeeded", "/old/machine/state/jobs/abc.bin", str(self.cwd)),
            )
            con.commit()
        self.key = self.root / "migration.key"

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **overrides):
        values = {
            "repo": str(self.repo), "state": str(self.state), "output": str(self.out),
            "key_file": str(self.key), "sealed_sha": self.sha,
            "ci_url": "https://ci.example.invalid/runs/42", "openssl": shutil.which("openssl"),
            "require_service_paused": True, "baseline_sha": "",
            "source_platform": "fixture-linux", "local_test_summary": "fixture suite green",
            "readiness_verified": True,
        }
        values.update(overrides)
        return type("Args", (), values)()

    def build(self):
        return builder.build(self.args())

    def prepare_context_state(self):
        (self.state / "harness.db").unlink()
        (self.state / "object-store.key").unlink()
        config = HarnessConfig(state_root=str(self.state))
        config.ensure()
        store = Store(config.database_path)
        events = EventBus(store)
        tasks = TaskRepository(store, events)
        objects = LocalAuthenticatedStorage(
            config.object_store_root, config.object_store_key,
            openssl_bin=config.openssl_bin,
        )
        execution = ExecutionStateRepository(store, events, tasks, objects, default_task_types())
        manager = ContextJobManager(config, store, execution)
        source = self.root / "context-source.txt"
        source.write_text("# Concepts\nMigration: relocatable context package\n", encoding="utf-8")
        job_id = manager.submit(str(source), name="Migration Context")
        result = manager.work_once()
        self.assertEqual(result["status"], "succeeded")
        return job_id, result["result"]["context_id"]

    def decrypt(self, encrypted: Path, output: Path):
        restorer.decrypt(str(shutil.which("openssl")), self.key, encrypted, output)

    def encrypt_untrusted_fixture(self, plain: Path, encrypted: Path):
        cipher = self.root / (plain.name + ".cipher")
        subprocess.run(
            [shutil.which("openssl"), "enc", "-aes-256-ctr", "-salt", "-pbkdf2", "-iter", "200000",
             "-pass", f"file:{self.key}", "-in", str(plain), "-out", str(cipher)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        body = restorer.ENVELOPE_MAGIC + cipher.read_bytes()
        mac = hmac.new(restorer.mac_key(self.key.read_bytes()), body, hashlib.sha256).digest()
        encrypted.write_bytes(body + mac)

    def test_builds_verified_package_without_plaintext_secrets(self):
        artifacts = self.build()
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        manifest = json.loads((self.out / "MANIFEST.json").read_text())
        self.assertEqual(manifest["source"]["sealed_commit_sha"], self.sha)
        plaintext = b"".join(
            path.read_bytes() for path in self.out.rglob("*")
            if path.is_file() and not path.name.endswith(".enc") and path.name != "repository.bundle"
        )
        self.assertNotIn(self.secret_value.encode(), plaintext)
        self.assertNotIn(b"JOB-KEY-SECRET", plaintext)
        self.assertIn(b"OPENAI_API_KEY=", (self.out / "config" / "env.example").read_bytes())
        self.assertNotIn(self.secret_value, json.dumps(manifest))
        self.assertNotIn(self.secret_value, (self.out / "reports" / "build-report.md").read_text())
        self.assertIn("not independently queried", (self.out / "reports" / "test-report.md").read_text())
        self.assertIn("PENDING EXTERNAL", (self.out / "reports" / "migration-readiness.md").read_text())
        self.assertTrue(artifacts["attestation"].is_file())
        attestation = json.loads(artifacts["attestation"].read_text())
        self.assertEqual(attestation["sealed_commit_sha"], self.sha)
        self.assertEqual(attestation["checks"]["package_internal_verifier"], "passed")
        subprocess.run(["git", "bundle", "verify", str(self.out / "repository.bundle")], cwd=self.repo, check=True, capture_output=True)
        subprocess.run([self.out / "verification" / "verify-package.sh", self.out, self.key], check=True, capture_output=True)
        without_key = subprocess.run(
            [self.out / "verification" / "verify-package.sh", self.out], capture_output=True,
        )
        self.assertNotEqual(without_key.returncode, 0)
        after_clone = (self.out / "verification" / "verify-after-clone.sh").read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover -s tests", after_clone)
        self.assertIn('python3 -m compileall -q harness2 tests', after_clone)
        self.assertTrue(artifacts["package_tar"].is_file())

    def test_sqlite_snapshot_and_restore_relocates_only_job_payload(self):
        artifacts = self.build()
        restored = self.root / "restored-state"
        result = restorer.restore(type("Args", (), {
            "archive": str(self.out / "private-state.tar.enc"), "key_file": str(self.key),
            "target": str(restored), "openssl": shutil.which("openssl"),
            "secrets_archive": str(artifacts["secret_transfer"]),
        })())
        self.assertEqual(result, restored)
        with closing(sqlite3.connect(restored / "harness.db")) as con:
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(con.execute("SELECT value FROM marker").fetchone(), ("snapshot-value",))
            payload = con.execute("SELECT payload_path FROM jobs WHERE id='abc'").fetchone()[0]
        self.assertEqual(payload, str(restored / "jobs" / "abc.bin"))
        self.assertEqual((restored / "jobs" / "abc.bin").read_bytes(), b"encrypted-job-payload")
        self.assertEqual(json.loads((restored / "secrets.json").read_text())["OPENAI_API_KEY"], self.secret_value)
        self.assertEqual((restored / "secrets.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((restored / "run").exists())

    def test_secret_and_emergency_archives_have_exact_scopes(self):
        artifacts = self.build()
        secret_tar = self.root / "secret.tar"
        emergency_tar = self.root / "emergency.tar"
        self.decrypt(artifacts["secret_transfer"], secret_tar)
        self.decrypt(artifacts["emergency"], emergency_tar)
        with tarfile.open(secret_tar) as archive:
            names = set(archive.getnames())
            content = b"".join(archive.extractfile(m).read() for m in archive.getmembers() if m.isfile())
        self.assertIn("secrets/secrets.json", names)
        self.assertIn(self.secret_value.encode(), content)
        with tarfile.open(emergency_tar) as archive:
            names = set(archive.getnames())
            content = b"".join(archive.extractfile(m).read() for m in archive.getmembers() if m.isfile())
        self.assertIn("source/app.py", names)
        self.assertIn("repository.bundle", names)
        self.assertIn("state/harness.db", names)
        self.assertIn("secrets/secrets.json", names)
        self.assertFalse(any(name.startswith(("state/run", "state/tmp", "state/logs", "state/caches")) for name in names))
        self.assertIn(self.secret_value.encode(), content)

    def test_normal_bundle_excludes_unselected_secret_branch(self):
        current = subprocess.run(
            ["git", "branch", "--show-current"], cwd=self.repo, check=True,
            text=True, capture_output=True,
        ).stdout.strip()
        subprocess.run(["git", "switch", "-q", "-c", "private-history"], cwd=self.repo, check=True)
        (self.repo / "retired.txt").write_text(self.secret_value, encoding="utf-8")
        subprocess.run(["git", "add", "retired.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "private branch"], cwd=self.repo, check=True)
        private_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
            text=True, capture_output=True,
        ).stdout.strip()
        subprocess.run(["git", "switch", "-q", current], cwd=self.repo, check=True)
        self.build()
        heads = subprocess.run(
            ["git", "bundle", "list-heads", str(self.out / "repository.bundle")],
            cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout.splitlines()
        self.assertEqual(heads, [f"{self.sha} HEAD"])
        self.assertFalse(any(private_sha in line for line in heads))

    def test_refuses_known_secret_in_sealed_history_even_after_removal(self):
        leak = self.repo / "leak.txt"
        leak.write_text(self.secret_value, encoding="utf-8")
        subprocess.run(["git", "add", "leak.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "accidental secret"], cwd=self.repo, check=True)
        leak.unlink()
        subprocess.run(["git", "add", "-u"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "remove accidental secret"], cwd=self.repo, check=True)
        self.sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
            text=True, capture_output=True,
        ).stdout.strip()
        with self.assertRaisesRegex(builder.PackageError, "known secret material"):
            self.build()

    def test_internal_and_external_checksums_detect_tampering(self):
        artifacts = self.build()
        for key in ("package_tar", "secret_transfer", "emergency", "attestation"):
            artifact = artifacts[key]
            expected, filename = Path(str(artifact) + ".sha256").read_text().strip().split("  ", 1)
            self.assertEqual(filename, artifact.name)
            self.assertEqual(expected, hashlib.sha256(artifact.read_bytes()).hexdigest())
        (self.out / "README.md").write_text("tampered", encoding="utf-8")
        result = subprocess.run(
            [self.out / "verification" / "verify-package.sh", self.out, self.key], capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_encrypted_state_authentication_detects_tampering(self):
        self.build()
        archive = self.out / "private-state.tar.enc"
        value = bytearray(archive.read_bytes())
        value[len(value) // 2] ^= 1
        archive.write_bytes(value)
        with self.assertRaisesRegex(restorer.RestoreError, "authentication failed"):
            restorer.verify_archive(type("Args", (), {
                "archive": str(archive), "key_file": str(self.key),
                "openssl": shutil.which("openssl"),
            })())

    def test_refuses_dirty_repo_and_sha_mismatch(self):
        (self.repo / "app.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(builder.PackageError, "dirty"):
            builder.build(self.args())
        subprocess.run(["git", "checkout", "--", "app.py"], cwd=self.repo, check=True)
        with self.assertRaisesRegex(builder.PackageError, "does not match"):
            builder.build(self.args(sealed_sha="0" * 40))

    def test_refuses_output_inside_repo_or_state(self):
        with self.assertRaisesRegex(builder.PackageError, "outside"):
            builder.build(self.args(output=str(self.repo / "package")))
        with self.assertRaisesRegex(builder.PackageError, "outside"):
            builder.build(self.args(output=str(self.state / "package")))

    def test_refuses_unknown_state_entry(self):
        (self.state / "mystery").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(builder.PackageError, "unknown"):
            builder.build(self.args())

    def test_refuses_symlink_anywhere_in_state(self):
        os.symlink(self.state / "jobs" / "abc.bin", self.state / "contexts" / "link")
        with self.assertRaisesRegex(builder.PackageError, "symlink"):
            builder.build(self.args())

    def test_refuses_live_service_when_pause_is_required(self):
        (self.state / "run" / "service.pid").write_text(str(os.getpid()), encoding="ascii")
        with self.assertRaisesRegex(builder.PackageError, "live service"):
            builder.build(self.args(require_service_paused=True))

    def test_refuses_snapshot_without_explicit_pause_requirement(self):
        with self.assertRaisesRegex(builder.PackageError, "require-service-paused"):
            builder.build(self.args(require_service_paused=False))

    def test_restore_refuses_nonterminal_job_with_unusable_cwd(self):
        with closing(sqlite3.connect(self.state / "harness.db")) as con:
            con.execute("UPDATE jobs SET status='queued',cwd='/path/that/does/not/exist'")
            con.commit()
        with self.assertRaisesRegex(builder.PackageError, "nonterminal legacy jobs"):
            self.build()

    def test_restore_allows_terminal_job_with_already_removed_payload(self):
        with closing(sqlite3.connect(self.state / "harness.db")) as con:
            con.execute("UPDATE jobs SET status='succeeded'")
            con.commit()
        (self.state / "jobs" / "abc.bin").unlink()
        self.build()
        restored = self.root / "terminal-restore"
        restorer.restore(type("Args", (), {
            "archive": str(self.out / "private-state.tar.enc"),
            "key_file": str(self.key), "target": str(restored),
            "openssl": shutil.which("openssl"),
        })())
        with closing(sqlite3.connect(restored / "harness.db")) as con:
            value = con.execute("SELECT payload_path FROM jobs WHERE id='abc'").fetchone()[0]
        self.assertEqual(value, str(restored / "jobs" / "abc.bin"))

    def test_context_job_package_and_snapshot_relocate_with_integrity(self):
        job_id, context_id = self.prepare_context_state()
        self.build()
        restored = self.root / "context-restored"
        restorer.restore(type("Args", (), {
            "archive": str(self.out / "private-state.tar.enc"),
            "key_file": str(self.key), "target": str(restored),
            "openssl": shutil.which("openssl"),
        })())
        job = json.loads((restored / "context-jobs" / f"{job_id}.json").read_text())
        self.assertEqual(job["result"]["package"], str(restored / "contexts" / context_id))
        package = ContextPackage.load(job["result"]["package"])
        self.assertEqual(package.ir.context_id, context_id)

    def test_context_package_tamper_blocks_snapshot(self):
        _job_id, context_id = self.prepare_context_state()
        with (self.state / "contexts" / context_id / "ir.json").open("ab") as stream:
            stream.write(b" ")
        with self.assertRaisesRegex(builder.PackageError, "context package"):
            self.build()

    def test_state_checksum_inventory_is_exact(self):
        root = self.root / "checksum-state"
        root.mkdir()
        (root / "one").write_bytes(b"one")
        (root / "extra").write_bytes(b"extra")
        (root / "STATE_CHECKSUMS.sha256").write_text(
            hashlib.sha256(b"one").hexdigest() + "  one\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(restorer.RestoreError, "exactly match"):
            restorer.verify_state_checksums(root)

    def test_existing_migration_key_requires_private_mode(self):
        self.key.write_text("a" * 64 + "\n", encoding="ascii")
        os.chmod(self.key, 0o644)
        with self.assertRaisesRegex(builder.PackageError, "mode 0600"):
            self.build()

    def test_restore_rejects_traversal_and_symlink_archives(self):
        for kind in ("traversal", "symlink"):
            plain = self.root / f"{kind}.tar"
            with tarfile.open(plain, "w") as archive:
                info = tarfile.TarInfo("state/../../escape" if kind == "traversal" else "state/link")
                if kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/tmp"
                else:
                    info.size = 1
                import io
                archive.addfile(info, None if kind == "symlink" else io.BytesIO(b"x"))
            encrypted = self.root / f"{kind}.enc"
            self.key.write_text("a" * 64 + "\n", encoding="ascii")
            os.chmod(self.key, 0o600)
            self.encrypt_untrusted_fixture(plain, encrypted)
            with self.assertRaises(restorer.RestoreError):
                restorer.restore(type("Args", (), {
                    "archive": str(encrypted), "key_file": str(self.key),
                    "target": str(self.root / f"restore-{kind}"), "openssl": shutil.which("openssl"),
                })())

    def test_restore_rejects_runtime_or_unknown_state_categories(self):
        plain = self.root / "unknown-state.tar"
        with tarfile.open(plain, "w") as archive:
            import io
            info = tarfile.TarInfo("state/run/service.pid")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"123"))
        encrypted = self.root / "unknown-state.enc"
        self.key.write_text("a" * 64 + "\n", encoding="ascii")
        os.chmod(self.key, 0o600)
        self.encrypt_untrusted_fixture(plain, encrypted)
        with self.assertRaisesRegex(restorer.RestoreError, "unexpected archive path"):
            restorer.restore(type("Args", (), {
                "archive": str(encrypted), "key_file": str(self.key),
                "target": str(self.root / "restore-unknown"),
                "openssl": shutil.which("openssl"),
            })())

    def test_restore_rejects_symlinked_archive_and_key(self):
        self.build()
        archive_link = self.root / "archive-link"
        key_link = self.root / "key-link"
        os.symlink(self.out / "private-state.tar.enc", archive_link)
        os.symlink(self.key, key_link)
        for archive, key in ((archive_link, self.key), (self.out / "private-state.tar.enc", key_link)):
            with self.subTest(archive=archive, key=key), self.assertRaises(restorer.RestoreError):
                restorer.verify_archive(type("Args", (), {
                    "archive": str(archive), "key_file": str(key),
                    "openssl": shutil.which("openssl"),
                })())


if __name__ == "__main__":
    unittest.main()
