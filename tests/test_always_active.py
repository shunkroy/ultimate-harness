"""Always-active heartbeat truthfulness and bounded service-cycle tests."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from harness2.config import HarnessConfig
from harness2.security import atomic_write_json
from harness2.service import ServiceLoop, active_status, service_process_matches
from harness2.store import Store


class AlwaysActiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.config = HarnessConfig(state_root=os.path.join(self.tmp.name, "state"))
        self.config.ensure()
        self.store = Store(self.config.database_path)

    def test_default_is_desired_but_not_falsely_active(self):
        value = active_status(self.config)
        self.assertTrue(value["desired_always_active"])
        self.assertFalse(value["active"])
        self.assertEqual(value["observed_state"], "configured")

    def test_fresh_heartbeat_requires_exact_process_identity(self):
        pidfile = os.path.join(self.config.state_root, "run", "service.pid")
        from harness2 import supervisor
        supervisor.write_pidfile(pidfile, 1234)
        atomic_write_json(self.config.service_heartbeat, {
            "service_pid": 1234, "heartbeat_at": time.time(), "cycles": 2,
            "observed_state": "active", "last_work_type": None,
            "last_work_id": None, "last_error": "",
        })
        package_root = self.config.package_root
        realpath = os.path.realpath
        with patch("harness2.service.supervisor.pid_alive", return_value=True), patch(
            "harness2.service.supervisor.read_cmdline",
            return_value=[self.config.python_bin, "-m", "harness2", "supervise"],
        ), patch(
            "harness2.service.os.path.realpath",
            side_effect=lambda value, **kwargs: package_root if value == "/proc/1234/cwd" else realpath(value, **kwargs),
        ):
            value = active_status(self.config)
        self.assertTrue(value["active"])
        self.assertTrue(value["heartbeat_fresh"])

    def test_stale_heartbeat_is_not_active(self):
        pidfile = os.path.join(self.config.state_root, "run", "service.pid")
        from harness2 import supervisor
        supervisor.write_pidfile(pidfile, 1234)
        atomic_write_json(self.config.service_heartbeat, {
            "service_pid": 1234, "heartbeat_at": time.time() - 999,
            "cycles": 2, "observed_state": "active",
        })
        package_root = self.config.package_root
        realpath = os.path.realpath
        with patch("harness2.service.supervisor.pid_alive", return_value=True), patch(
            "harness2.service.supervisor.read_cmdline",
            return_value=[self.config.python_bin, "-m", "harness2", "supervise"],
        ), patch(
            "harness2.service.os.path.realpath",
            side_effect=lambda value, **kwargs: package_root if value == "/proc/1234/cwd" else realpath(value, **kwargs),
        ):
            value = active_status(self.config, freshness=60)
        self.assertFalse(value["active"])
        self.assertEqual(value["observed_state"], "stale")

    def test_console_script_process_identity_is_recognized(self):
        launcher = os.path.join(self.tmp.name, "harness")
        with open(launcher, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env python3\n")
        os.chmod(launcher, 0o700)
        with patch.dict(os.environ, {"HARNESS_LAUNCHER": launcher}), patch(
            "harness2.service.supervisor.pid_alive", return_value=True,
        ), patch(
            "harness2.service.supervisor.read_cmdline",
            return_value=[self.config.python_bin, launcher, "supervise", "--interval", "30"],
        ):
            self.assertTrue(service_process_matches(self.config, 1234))

    def test_service_cycle_processes_bounded_run_and_context_work(self):
        prime = Mock()
        prime.config = self.config
        jobs = Mock()
        jobs.work_once.return_value = {"id": "run1", "status": "succeeded"}
        context_jobs = Mock()
        context_jobs.work_once.return_value = {"id": "ctx1", "status": "succeeded"}
        loop = ServiceLoop(self.config, self.store, prime, jobs, context_jobs, interval=1)
        sleeps = 0

        def stop_after_one(_):
            nonlocal sleeps
            sleeps += 1
            loop.running = False

        with patch("harness2.config.HarnessConfig.hardened_prime_available", new_callable=unittest.mock.PropertyMock, return_value=False), patch(
            "harness2.service.time.sleep", side_effect=stop_after_one,
        ):
            loop.run()
        jobs.work_once.assert_called_once()
        context_jobs.work_once.assert_called_once()
        self.assertEqual(loop.cycles, 1)

    def test_bootstrap_publishes_starting_heartbeat_before_loop(self):
        loop = ServiceLoop.bootstrap(
            self.config, self.store, Mock(), Mock(), Mock(), interval=1,
        )
        from harness2.security import read_private_json
        heartbeat = read_private_json(self.config.service_heartbeat)
        self.assertEqual(heartbeat["observed_state"], "starting")
        self.assertEqual(heartbeat["service_pid"], os.getpid())
        self.assertEqual(heartbeat["boot_id"], loop.boot_id)


if __name__ == "__main__":
    unittest.main()
