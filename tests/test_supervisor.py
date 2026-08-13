"""Tests for :mod:`harness2.supervisor` (stdlib `unittest`, no deps).

Covers, per the spec: run-dir validation (symlink / owner / mode 0700),
atomic pidfile I/O (mode 0600, symlink rejection), exact cmdline matching
(helper + real /proc processes), truthful AF_UNIX socket probe (real connect,
not existence), flock/mkdir lock exclusivity, stale cleanup (removal, keep,
heal, symlink refusal), and the start/stop round-trip against a fake daemon
(a real python subprocess standing in for the node dist bundle).
"""

from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HARNESS_ROOT not in sys.path:
    sys.path.insert(0, HARNESS_ROOT)

from harness2 import supervisor as sup  # noqa: E402

PY = sys.executable

# Fake daemon: real subprocess whose argv exactly contains the bundle path and
# the "--daemon-socket <path>" pair, binds an AF_UNIX listener, and exits
# cleanly on SIGTERM. Stands in for the node dist bundle.
FAKE_DAEMON = """\
import os, signal, socket, sys, time
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
args = sys.argv[1:]
sock = None
for i, a in enumerate(args):
    if a == "--daemon-socket" and i + 1 < len(args):
        sock = args[i + 1]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
if sock:
    try:
        s.bind(sock)
        s.listen(16)
    except OSError:
        pass
print("ready", flush=True)
while True:
    time.sleep(1)
"""

DIE_FAST = "import sys\nsys.exit(3)\n"


class SupervisorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # ignore_cleanup_errors: under PRoot/FUSE a failed AF_UNIX connect() to
        # a regular-file path can transiently corrupt the dentry (see
        # test_socket_probe_plain_file_is_false); tolerant teardown keeps such
        # an environment quirk from failing the suite.
        self._tmp = tempfile.TemporaryDirectory(prefix="harness2-test-",
                                                ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self._procs: list[subprocess.Popen] = []
        self.addCleanup(self._kill_all)

    # -- helpers ------------------------------------------------------------

    def path(self, *parts: str) -> str:
        return os.path.join(self.base, *parts)

    def run_dir(self) -> str:
        d = self.path("run")
        os.mkdir(d, 0o700)
        return d

    def socket_path(self) -> str:
        return self.path("daemon.sock")

    def bundle(self, code: str = FAKE_DAEMON, name: str = "bundle.js") -> str:
        p = self.path(name)
        with open(p, "w") as fh:
            fh.write(code)
        return p

    def spawn(self, *argv: str, **kw) -> subprocess.Popen:
        p = subprocess.Popen(list(argv), **kw)
        self._procs.append(p)
        return p

    def dead_pid(self) -> int:
        procs = {int(e) for e in os.listdir("/proc") if e.isdigit()}
        pid = 10**6
        while pid in procs:
            pid += 1
        return pid

    def _kill_all(self) -> None:
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in self._procs:
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=3)
                except Exception:
                    pass

    def spawn_fake_daemon(self, bundle: str, sock: str) -> subprocess.Popen:
        return self.spawn(PY, bundle, "--daemon-socket", sock,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # -- run dir validation -------------------------------------------------

    def test_validate_run_dir_ok(self) -> None:
        d = self.run_dir()
        self.assertEqual(sup.validate_run_dir(d), os.path.abspath(d))

    def test_ensure_run_dir_creates_0700(self) -> None:
        d = self.path("fresh-run")
        created = sup.ensure_run_dir(d)
        self.assertEqual(created, os.path.abspath(d))
        st = os.lstat(d)
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)
        # idempotent
        sup.ensure_run_dir(d)

    def test_validate_run_dir_rejects_symlink(self) -> None:
        real = self.run_dir()
        link = self.path("run-link")
        os.symlink(real, link)
        with self.assertRaises(sup.RunDirError):
            sup.validate_run_dir(link)

    def test_validate_run_dir_rejects_file(self) -> None:
        f = self.path("not-a-dir")
        with open(f, "w") as fh:
            fh.write("x")
        with self.assertRaises(sup.RunDirError):
            sup.validate_run_dir(f)

    def test_validate_run_dir_rejects_mode(self) -> None:
        d = self.run_dir()
        os.chmod(d, 0o755)
        with self.assertRaises(sup.RunDirError):
            sup.validate_run_dir(d)

    def test_validate_run_dir_rejects_wrong_owner(self) -> None:
        d = self.run_dir()
        try:
            os.chown(d, 12345, -1)
        except OSError:
            self.skipTest("chown not permitted in this environment")
        if os.lstat(d).st_uid == os.getuid():
            self.skipTest("chown had no effect (privileged/emulated fs)")
        with self.assertRaises(sup.RunDirError):
            sup.validate_run_dir(d)

    # -- pidfile ------------------------------------------------------------

    def test_pidfile_roundtrip_mode_and_atomicity(self) -> None:
        pidfile = self.path("run", "daemon.pid")
        os.mkdir(self.path("run"))
        sup.write_pidfile(pidfile, 4242)
        self.assertEqual(sup.read_pidfile(pidfile), 4242)
        self.assertEqual(stat.S_IMODE(os.lstat(pidfile).st_mode), 0o600)
        # no leftover temp files after atomic replace
        leftovers = [e for e in os.listdir(self.path("run"))
                     if ".tmp" in e]
        self.assertEqual(leftovers, [])

    def test_pidfile_missing_returns_none(self) -> None:
        self.assertIsNone(sup.read_pidfile(self.path("nope", "daemon.pid")))
        self.assertIsNone(sup.read_pidfile(self.path("missing.pid")))

    def test_pidfile_corrupt_returns_none(self) -> None:
        pidfile = self.path("bad.pid")
        with open(pidfile, "w") as fh:
            fh.write("not-a-number\n")
        self.assertIsNone(sup.read_pidfile(pidfile))

    def test_pidfile_rejects_symlink(self) -> None:
        target = self.path("target")
        with open(target, "w") as fh:
            fh.write("123\n")
        link = self.path("linked.pid")
        os.symlink(target, link)
        with self.assertRaises(sup.SecurityError):
            sup.read_pidfile(link)

    def test_safe_unlink_refuses_symlink_and_dir(self) -> None:
        target = self.path("victim")
        with open(target, "w") as fh:
            fh.write("x")
        link = self.path("link")
        os.symlink(target, link)
        with self.assertRaises(sup.SecurityError):
            sup.safe_unlink(link)
        self.assertTrue(os.path.exists(target))  # symlink untouched, target safe

        d = self.path("adir")
        os.mkdir(d)
        with self.assertRaises(sup.SecurityError):
            sup.safe_unlink(d)
        self.assertTrue(os.path.isdir(d))

    def test_safe_unlink_removes_file_and_socket(self) -> None:
        f = self.path("plain")
        with open(f, "w") as fh:
            fh.write("x")
        sup.safe_unlink(f)
        self.assertFalse(os.path.lexists(f))

        s = self.path("stale.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(s)
        sock.close()  # path remains as a dead socket inode
        sup.safe_unlink(s)
        self.assertFalse(os.path.lexists(s))

    # -- exact cmdline matching --------------------------------------------

    def test_argv_matches_exact(self) -> None:
        bundle = self.bundle()
        sock = self.socket_path()
        argv = [PY, bundle, "--daemon-socket", sock]

        self.assertTrue(sup.argv_matches(argv, bundle, sock))
        self.assertFalse(sup.argv_matches(argv, self.path("other.js"), sock))
        self.assertFalse(sup.argv_matches(argv, bundle, self.path("other.sock")))
        # bare basename is not an exact path match
        self.assertFalse(sup.argv_matches(argv, os.path.basename(bundle), sock))
        # same bundle, no socket entry
        self.assertFalse(sup.argv_matches([PY, bundle], bundle, sock))
        self.assertFalse(sup.argv_matches([], bundle, sock))

    def test_argv_matches_realpath_normalization(self) -> None:
        bundle = self.bundle()
        sock = self.socket_path()
        linkdir = self.path("lnk")
        os.mkdir(linkdir)
        os.symlink(bundle, os.path.join(linkdir, "bundle.js"))
        argv = [PY, os.path.join(linkdir, "bundle.js"), "--daemon-socket", sock]
        # argv went through a symlink; realpath normalization still matches
        self.assertTrue(sup.argv_matches(argv, bundle, sock))

    def test_cmdline_matches_real_process(self) -> None:
        bundle = self.bundle()
        sock = self.socket_path()
        proc = self.spawn_fake_daemon(bundle, sock)
        # wait until /proc is populated
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if sup.cmdline_matches(proc.pid, bundle, sock):
                break
            time.sleep(0.05)
        self.assertTrue(sup.cmdline_matches(proc.pid, bundle, sock))
        self.assertFalse(sup.cmdline_matches(proc.pid, bundle, self.path("x.sock")))
        self.assertFalse(sup.cmdline_matches(proc.pid, self.path("y.js"), sock))
        self.assertEqual(sup.find_matching_pids(bundle, sock), [proc.pid])

        proc.terminate()
        proc.wait(timeout=5)
        self.assertFalse(sup.cmdline_matches(proc.pid, bundle, sock))
        self.assertEqual(sup.find_matching_pids(bundle, sock), [])

    # -- socket reachability probe -----------------------------------------

    def test_socket_probe_listening(self) -> None:
        sock = self.socket_path()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind(sock)
        s.listen(8)
        self.assertTrue(sup.socket_reachable(sock))

    def test_socket_probe_closed_socket_is_false(self) -> None:
        sock = self.socket_path()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(sock)
        s.close()  # stale inode: path exists, no listener
        self.assertTrue(os.path.exists(sock))
        self.assertFalse(sup.socket_reachable(sock))

    def test_socket_probe_plain_file_is_false(self) -> None:
        # Truthful probe: the target EXISTS as a regular file but is not a
        # listening socket, so connect() must fail even though the path is
        # present. The probe goes through /proc/self/fd/<n>: connecting an
        # AF_UNIX stream socket directly to a regular-file *path* under
        # PRoot/FUSE corrupts that dentry into a permanent phantom entry
        # (stat fails, entry lingers, rmtree errors), so routing via procfs
        # keeps the file's own dentry untouched while still performing a real
        # connect() against a real, existing regular file.
        f = self.path("not-a-socket")
        with open(f, "w") as fh:
            fh.write("x")
        fd = os.open(f, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fdpath = f"/proc/self/fd/{fd}"
        self.assertTrue(os.path.exists(fdpath))  # target really exists
        self.assertFalse(sup.socket_reachable(fdpath))
        self.assertTrue(os.path.exists(f))  # dentry intact, teardown clean

    def test_socket_probe_missing_is_false(self) -> None:
        self.assertFalse(sup.socket_reachable(self.path("never.sock")))

    # -- start mutex --------------------------------------------------------

    def test_flock_lock_exclusive_same_process(self) -> None:
        lockfile = self.path("run", ".start.lock")
        os.mkdir(self.path("run"))
        first = sup.FlockLock(lockfile, timeout=0.5)
        first.acquire()
        try:
            self.assertTrue(first.is_held())
            with self.assertRaises(sup.LockTimeoutError):
                sup.FlockLock(lockfile, timeout=0.3).acquire()
        finally:
            first.release()
        self.assertFalse(first.is_held())
        # re-acquirable after release
        sup.FlockLock(lockfile, timeout=0.5).acquire().release()

    def test_flock_lock_exclusive_cross_process(self) -> None:
        lockfile = self.path("cross.lock")
        holder = self.spawn(
            PY, "-c",
            "import fcntl,sys,time\n"
            "f=open(sys.argv[1],'a+')\n"
            "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            "print('LOCKED', flush=True)\n"
            "time.sleep(20)\n",
            lockfile,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
        with self.assertRaises(sup.LockTimeoutError):
            sup.FlockLock(lockfile, timeout=1.0).acquire()
        holder.terminate()
        holder.wait(timeout=5)
        holder.stdout.close()
        # released when the holder died: we can acquire now
        acquired = sup.FlockLock(lockfile, timeout=2.0).acquire()
        acquired.release()

    def test_mkdir_lock_exclusive_same_process(self) -> None:
        lockdir = self.path("run", ".start.lock")
        os.mkdir(self.path("run"))
        first = sup.MkdirLock(lockdir, timeout=0.5)
        first.acquire()
        try:
            self.assertTrue(first.is_held())
            with self.assertRaises(sup.LockTimeoutError):
                sup.MkdirLock(lockdir, timeout=0.3).acquire()
        finally:
            first.release()
        self.assertFalse(first.is_held())
        self.assertFalse(os.path.exists(lockdir))

    def test_mkdir_lock_stale_steal(self) -> None:
        lockdir = self.path("run", ".start.lock")
        os.mkdir(self.path("run"))
        os.mkdir(lockdir)  # abandoned lock from a dead holder
        old = time.time() - 1000
        os.utime(lockdir, (old, old))
        acquired = sup.MkdirLock(lockdir, timeout=0.5, stale_after=60).acquire()
        self.assertTrue(acquired.is_held())
        acquired.release()

    def test_acquire_start_lock_auto_and_forced(self) -> None:
        rd = self.run_dir()
        flock_lock = sup.acquire_start_lock(rd, name="flock.lock",
                                            method="flock", timeout=1.0)
        try:
            self.assertTrue(flock_lock.is_held())
            self.assertIsInstance(flock_lock, sup.FlockLock)
        finally:
            flock_lock.release()
        mkdir_lock = sup.acquire_start_lock(rd, name="mkdir.lock",
                                            method="mkdir", timeout=1.0)
        try:
            self.assertTrue(mkdir_lock.is_held())
            self.assertIsInstance(mkdir_lock, sup.MkdirLock)
        finally:
            mkdir_lock.release()
        auto_lock = sup.acquire_start_lock(rd, timeout=1.0)
        try:
            self.assertTrue(auto_lock.is_held())
        finally:
            auto_lock.release()

    def test_auto_lock_uses_mkdir_when_fcntl_is_unavailable(self) -> None:
        rd = self.run_dir()
        with mock.patch.object(sup, "fcntl", None):
            lock = sup.acquire_start_lock(rd, name="portable.lock", method="auto", timeout=1.0)
            try:
                self.assertIsInstance(lock, sup.MkdirLock)
            finally:
                lock.release()

    def test_lock_methods_adapt_to_existing_lock(self) -> None:
        # A flock lock creates a lock FILE; a mkdir-preferring acquirer on the
        # SAME name must adapt and contend on the file (mutual exclusion
        # across strategies), not deadlock on FileExistsError.
        rd = self.run_dir()
        a = sup.acquire_start_lock(rd, name="shared.lock", method="flock",
                                   timeout=1.0)
        try:
            self.assertTrue(a.is_held())
            with self.assertRaises(sup.LockTimeoutError):
                sup.acquire_start_lock(rd, name="shared.lock", method="mkdir",
                                       timeout=0.3)
        finally:
            a.release()
        b = sup.acquire_start_lock(rd, name="shared.lock", method="mkdir",
                                   timeout=1.0)
        try:
            self.assertTrue(b.is_held())
        finally:
            b.release()

    # -- stale cleanup ------------------------------------------------------

    def test_cleanup_removes_stale_pidfile_and_socket(self) -> None:
        rd = self.run_dir()
        pidfile = self.path("run", "daemon.pid")
        sup.write_pidfile(pidfile, self.dead_pid())
        sock = self.socket_path()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind(sock)
        s.close()  # dead socket inode

        actions = sup.cleanup_stale(rd, self.bundle(), sock)
        self.assertFalse(os.path.lexists(pidfile))
        self.assertFalse(os.path.lexists(sock))
        self.assertTrue(any("pidfile" in a for a in actions))
        self.assertTrue(any("socket" in a for a in actions))

    def test_cleanup_keeps_live_daemon(self) -> None:
        rd = self.run_dir()
        bundle = self.bundle()
        sock = self.socket_path()
        proc = self.spawn_fake_daemon(bundle, sock)
        self.wait_socket(sock)
        sup.write_pidfile(self.path("run", "daemon.pid"), proc.pid)

        actions = sup.cleanup_stale(rd, bundle, sock)
        self.assertEqual(actions, [])
        self.assertEqual(sup.read_pidfile(self.path("run", "daemon.pid")),
                         proc.pid)
        self.assertTrue(os.path.lexists(sock))

    def test_cleanup_removes_corrupt_pidfile(self) -> None:
        rd = self.run_dir()
        pidfile = self.path("run", "daemon.pid")
        with open(pidfile, "w") as fh:
            fh.write("garbage\n")
        actions = sup.cleanup_stale(rd, self.bundle(), self.socket_path())
        self.assertFalse(os.path.lexists(pidfile))
        self.assertTrue(any("pidfile" in a for a in actions))

    def test_cleanup_heals_stale_pidfile_to_live_pid(self) -> None:
        rd = self.run_dir()
        bundle = self.bundle()
        sock = self.socket_path()
        proc = self.spawn_fake_daemon(bundle, sock)
        self.wait_socket(sock)
        pidfile = self.path("run", "daemon.pid")
        sup.write_pidfile(pidfile, self.dead_pid())  # stale entry

        actions = sup.cleanup_stale(rd, bundle, sock)
        self.assertTrue(any("healed" in a for a in actions))
        self.assertEqual(sup.read_pidfile(pidfile), proc.pid)
        self.assertTrue(os.path.lexists(sock))

    def test_cleanup_refuses_symlink_socket(self) -> None:
        rd = self.run_dir()
        target = self.path("innocent")
        with open(target, "w") as fh:
            fh.write("x")
        link = self.socket_path()
        os.symlink(target, link)
        with self.assertRaises(sup.SecurityError):
            sup.cleanup_stale(rd, self.bundle(), link)
        self.assertTrue(os.path.lexists(target))  # target untouched

    # -- start / stop -------------------------------------------------------

    def test_start_stop_roundtrip(self) -> None:
        rd = self.run_dir()
        bundle = self.bundle()
        sock = self.socket_path()
        log = self.path("daemon.log")

        st = sup.start_daemon(
            bundle_path=bundle, run_dir=rd, socket_path=sock, log_path=log,
            node=PY, wait_socket=10, grace=3, lock_timeout=5,
        )
        self.assertTrue(st.running, st)
        self.assertTrue(st.socket_reachable)
        self.assertTrue(st.pid is not None and st.pid > 1)
        # pidfile mode + content
        self.assertEqual(stat.S_IMODE(os.lstat(st.pidfile_path).st_mode), 0o600)
        self.assertEqual(sup.read_pidfile(st.pidfile_path), st.pid)
        # log file private 0600 and captured the daemon's stdout
        self.assertEqual(stat.S_IMODE(os.lstat(log).st_mode), 0o600)
        with open(log) as fh:
            self.assertIn("ready", fh.read())

        # status matches
        st2 = sup.status(run_dir=rd, bundle_path=bundle, socket_path=sock,
                         log_path=log)
        self.assertEqual(st2.pid, st.pid)
        self.assertTrue(st2.running)
        self.assertTrue(st2.healthy)

        stop = sup.stop_daemon(
            run_dir=rd, bundle_path=bundle, socket_path=sock,
            grace=3, kill_wait=2,
        )
        self.assertEqual(stop.stop_reason, "term")
        self.assertFalse(stop.running)
        self.assertFalse(stop.socket_reachable)
        self.assertFalse(os.path.lexists(st.pidfile_path))
        self.assertFalse(os.path.lexists(sock))

    def test_start_single_instance(self) -> None:
        rd = self.run_dir()
        bundle = self.bundle()
        sock = self.socket_path()
        first = sup.start_daemon(bundle_path=bundle, run_dir=rd,
                                 socket_path=sock, node=PY,
                                 wait_socket=10, grace=3, lock_timeout=5)
        second = sup.start_daemon(bundle_path=bundle, run_dir=rd,
                                  socket_path=sock, node=PY,
                                  wait_socket=10, grace=3, lock_timeout=5)
        self.assertEqual(second.pid, first.pid)
        self.assertEqual(sup.find_matching_pids(bundle, sock), [first.pid])
        sup.stop_daemon(run_dir=rd, bundle_path=bundle, socket_path=sock,
                        grace=3, kill_wait=2)

    def test_start_early_exit_raises_and_cleans(self) -> None:
        rd = self.run_dir()
        bundle = self.bundle(code=DIE_FAST)
        sock = self.socket_path()
        with self.assertRaises(sup.StartError):
            sup.start_daemon(bundle_path=bundle, run_dir=rd, socket_path=sock,
                             node=PY, wait_socket=3, grace=1, lock_timeout=5)
        self.assertFalse(os.path.lexists(self.path("run", "daemon.pid")))
        self.assertFalse(os.path.lexists(sock))

    def test_stop_refuses_unrelated_pid(self) -> None:
        rd = self.run_dir()
        bundle = self.bundle()
        sock = self.socket_path()
        # unrelated long-lived process (different argv -> no exact match)
        unrelated = self.spawn(PY, "-c", "import time; time.sleep(30)")
        pidfile = self.path("run", "daemon.pid")
        sup.write_pidfile(pidfile, unrelated.pid)

        st = sup.stop_daemon(run_dir=rd, bundle_path=bundle, socket_path=sock,
                             grace=2, kill_wait=1)
        # the unrelated process must be untouched
        self.assertEqual(unrelated.poll(), None)
        self.assertTrue(sup.pid_alive(unrelated.pid))
        # and the bogus pidfile is treated as stale and removed
        self.assertFalse(os.path.lexists(pidfile))
        self.assertFalse(st.running)

    def test_stop_force_kills_matching_orphan(self) -> None:
        rd = self.run_dir()
        bundle = self.bundle()
        sock = self.socket_path()
        proc = self.spawn_fake_daemon(bundle, sock)
        self.wait_socket(sock)
        # no pidfile -> orphan; force mode must find it by exact argv
        st = sup.stop_daemon(run_dir=rd, bundle_path=bundle, socket_path=sock,
                             grace=2, kill_wait=2, force=True)
        proc.wait(timeout=5)
        self.assertIn(st.stop_reason, ("term", "kill"))
        self.assertFalse(st.running)

    def test_status_missing_run_dir(self) -> None:
        st = sup.status(run_dir=self.path("does-not-exist"),
                        bundle_path=self.bundle())
        self.assertFalse(st.running)
        self.assertIsNotNone(st.error)
        self.assertIn("run dir", st.error)

    # -- helpers used by tests ----------------------------------------------

    def wait_socket(self, sock: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sup.socket_reachable(sock):
                return
            time.sleep(0.05)
        self.fail(f"socket {sock} never became reachable")


if __name__ == "__main__":
    unittest.main()
