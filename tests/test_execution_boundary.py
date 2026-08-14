"""Checkpoint 3C tests for the stdlib-only execution boundary."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from harness2.execution import (
    BodyLimitExceeded,
    MAX_OUTPUT_BYTES,
    MAX_TIMEOUT_SECONDS,
    ProcessConfigurationError,
    ProcessRequest,
    ProcessSpawnError,
    prepare_working_directory,
    read_bounded_body,
    run_process,
)


class ExecutionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.cwd = self.temp.name

    def request(self, code: str, **kwargs: object) -> ProcessRequest:
        values: dict[str, object] = {
            "argv": (sys.executable, "-c", code),
            "env": {},
            "cwd": self.cwd,
            "timeout": 3,
            "stdout_limit": 4096,
            "stderr_limit": 4096,
        }
        values.update(kwargs)
        return ProcessRequest(**values)  # type: ignore[arg-type]

    def test_success_exit_code_and_capture(self) -> None:
        result = run_process(
            self.request(
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"
            )
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")
        self.assertGreaterEqual(result.duration, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.output_limited)
        self.assertEqual(len(result.config_fingerprint), 64)

    def test_fingerprint_is_stable_and_redacts_private_values(self) -> None:
        code = "pass"
        first_path = os.path.join(self.cwd, "private-one")
        second_path = os.path.join(self.cwd, "private-two")
        first = self.request(
            code,
            argv=(sys.executable, "-c", code, first_path),
            env={"PUBLIC": "same", "TOKEN": "top-secret-one"},
            private_argv_indices=(3,),
            secret_env_keys=("TOKEN",),
        )
        second = self.request(
            code,
            argv=(sys.executable, "-c", code, second_path),
            env={"TOKEN": "top-secret-two", "PUBLIC": "same"},
            private_argv_indices=(3,),
            secret_env_keys=("TOKEN",),
        )
        self.assertEqual(first.config_fingerprint, second.config_fingerprint)
        rendered = repr(first)
        self.assertNotIn(first_path, rendered)
        self.assertNotIn("top-secret-one", rendered)
        self.assertIn("<private>", rendered)

        public_change = self.request(
            code,
            argv=(sys.executable, "-c", code, second_path),
            env={"PUBLIC": "changed", "TOKEN": "top-secret-two"},
            private_argv_indices=(3,),
            secret_env_keys=("TOKEN",),
        )
        self.assertNotEqual(first.config_fingerprint, public_change.config_fingerprint)

    def test_environment_is_copied_read_only_and_request_is_frozen(self) -> None:
        source = {"A": "one"}
        request = self.request("pass", env=source)
        source["A"] = "changed"
        self.assertEqual(request.env["A"], "one")
        with self.assertRaises(TypeError):
            request.env["A"] = "two"  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            request.timeout = 2  # type: ignore[misc]
        self.assertIsInstance(request.argv, tuple)

    def test_cwd_is_canonical_existing_directory(self) -> None:
        child = Path(self.cwd, "child")
        child.mkdir()
        request = self.request("pass", cwd=child / ".." / "child")
        self.assertEqual(request.cwd, str(child.resolve()))
        self.assertEqual(request.cwd_identity, (child.stat().st_dev, child.stat().st_ino))

    def test_prepared_cwd_rejects_identity_retarget(self) -> None:
        first = Path(self.cwd, "first")
        second = Path(self.cwd, "second")
        first.mkdir()
        second.mkdir()
        link = Path(self.cwd, "workspace")
        link.symlink_to(first, target_is_directory=True)
        canonical, identity = prepare_working_directory(link)
        link.unlink()
        first.rename(Path(self.cwd, "moved-first"))
        Path(canonical).symlink_to(second, target_is_directory=True)
        with self.assertRaisesRegex(ProcessConfigurationError, "identity changed"):
            ProcessRequest(
                (sys.executable, "-c", "pass"), env={}, cwd=canonical,
                cwd_identity=identity, timeout=1, stdout_limit=100, stderr_limit=100,
            )

    def test_invalid_cwd_argv_and_limits(self) -> None:
        invalid_file = Path(self.cwd, "not-a-directory")
        invalid_file.write_text("x", encoding="utf-8")
        invalid_requests = (
            {"argv": []},
            {"argv": ()},
            {"argv": (sys.executable, "")},
            {"cwd": Path(self.cwd, "missing")},
            {"cwd": invalid_file},
            {"timeout": 0},
            {"timeout": MAX_TIMEOUT_SECONDS + 1},
            {"stdout_limit": 0},
            {"stderr_limit": MAX_OUTPUT_BYTES + 1},
            {"private_argv_indices": (99,)},
            {"env": {"TOKEN": "x"}, "secret_env_keys": ("MISSING",)},
        )
        for changes in invalid_requests:
            with self.subTest(changes=changes), self.assertRaises(ProcessConfigurationError):
                self.request("pass", **changes)

    def test_spawn_failure_is_typed_and_does_not_echo_private_executable(self) -> None:
        missing = os.path.join(self.cwd, "private-missing-executable")
        request = self.request(
            "pass",
            argv=(missing,),
            private_argv_indices=(0,),
        )
        with self.assertRaises(ProcessSpawnError) as caught:
            run_process(request)
        self.assertNotIn(missing, str(caught.exception))

    def test_timeout_terminates_process(self) -> None:
        started = time.monotonic()
        result = run_process(self.request("import time; time.sleep(30)", timeout=0.15))
        self.assertTrue(result.timed_out)
        self.assertFalse(result.output_limited)
        self.assertLess(time.monotonic() - started, 3)
        self.assertNotEqual(result.returncode, 0)

    def test_stdout_overflow_is_bounded(self) -> None:
        result = run_process(
            self.request("import os; os.write(1, b'x' * 100000)", stdout_limit=127)
        )
        self.assertTrue(result.output_limited)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stdout, "x" * 127)
        self.assertLessEqual(len(result.stderr.encode("utf-8")), 4096)

    def test_stderr_overflow_is_bounded(self) -> None:
        result = run_process(
            self.request("import os; os.write(2, b'y' * 100000)", stderr_limit=113)
        )
        self.assertTrue(result.output_limited)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stderr, "y" * 113)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)

    def test_malformed_utf8_uses_replacement_decoding(self) -> None:
        result = run_process(
            self.request("import os; os.write(1, b'good\\xffbad'); os.write(2, b'\\x80')")
        )
        self.assertEqual(result.stdout, "good\ufffdbad")
        self.assertEqual(result.stderr, "\ufffd")

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "POSIX /proc test")
    def test_pipe_holding_descendant_is_terminated(self) -> None:
        pid_file = Path(self.cwd, "descendant.pid")
        child_code = "import time; time.sleep(30)"
        parent_code = (
            "import os,subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
            "open(sys.argv[2],'w').write(str(p.pid)); os._exit(0)"
        )
        result = run_process(
            self.request(
                parent_code,
                argv=(sys.executable, "-c", parent_code, child_code, str(pid_file)),
            )
        )
        self.assertIn(result.returncode, (0, -signal.SIGTERM, -signal.SIGKILL))
        descendant_pid = int(pid_file.read_text(encoding="ascii"))

        def running_non_zombie(pid: int) -> bool:
            try:
                state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[2]
                return state != "Z"
            except (FileNotFoundError, ProcessLookupError):
                return False

        self.addCleanup(self._kill_if_alive, descendant_pid)
        deadline = time.monotonic() + 2
        while running_non_zombie(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(running_non_zombie(descendant_pid))

    @staticmethod
    def _kill_if_alive(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class BoundedBodyTests(unittest.TestCase):
    def test_bounded_body_reader(self) -> None:
        self.assertEqual(read_bounded_body(io.BytesIO(b"abcdef"), byte_limit=6), b"abcdef")
        with self.assertRaises(BodyLimitExceeded):
            read_bounded_body(io.BytesIO(b"abcdefg"), byte_limit=6, chunk_size=2)
        with self.assertRaises(ProcessConfigurationError):
            read_bounded_body(io.BytesIO(b""), byte_limit=0)


if __name__ == "__main__":
    unittest.main()
