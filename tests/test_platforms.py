"""Portable platform detection and launch primitives."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness2.platforms import PlatformKind, detect_platform, is_loopback_url
from harness2.config import HarnessConfig


class DetectionTests(unittest.TestCase):
    def test_windows(self):
        p = detect_platform(sys_platform="win32", os_name="nt", env={"USERPROFILE": "C:/U", "APPDATA": "C:/A", "LOCALAPPDATA": "C:/L"}, home="C:/U", release="10")
        self.assertEqual(p.kind, PlatformKind.WINDOWS)
        self.assertIn("Harness2", str(p.state_dir))
        self.assertFalse(p.supports_unix_supervision)

    def test_macos(self):
        p = detect_platform(sys_platform="darwin", os_name="posix", env={"HOME": "/Users/a"}, home="/Users/a", release="24")
        self.assertEqual(p.kind, PlatformKind.MACOS)
        self.assertIn("Application Support", str(p.state_dir))

    def test_linux_xdg(self):
        p = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": "/h", "XDG_STATE_HOME": "/state"}, home="/h", release="6.8")
        self.assertEqual(p.kind, PlatformKind.LINUX)
        self.assertEqual(str(p.state_dir), "/state/harness2")

    def test_termux(self):
        p = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": "/data/home", "PREFIX": "/data/data/com.termux/files/usr"}, home="/data/home", release="6")
        self.assertEqual(p.kind, PlatformKind.TERMUX)
        self.assertEqual(p.platform_id, "android-termux")
        self.assertEqual(p.capability_map()["android_bridge"], "not_implemented")

    def test_proot_precedes_termux(self):
        with tempfile.TemporaryDirectory() as home:
            p = detect_platform(
                sys_platform="linux", os_name="posix",
                env={"HOME": home, "PREFIX": "/data/data/com.termux/files/usr", "PROOT_L2S_DIR": os.path.join(home, "proot")},
                home=home, release="6-PRoot-Distro",
            )
            self.assertEqual(p.kind, PlatformKind.PROOT)

    def test_inaccessible_legacy_path_does_not_abort_detection(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = os.path.join(home, "state")
            with patch("harness2.platforms.Path.exists", side_effect=PermissionError("denied")):
                p = detect_platform(
                    sys_platform="linux", os_name="posix",
                    env={"HOME": home, "XDG_STATE_HOME": state_root},
                    home=home, release="6.8",
                )
            self.assertEqual(p.kind, PlatformKind.LINUX)
            self.assertEqual(str(p.state_dir), os.path.join(state_root, "harness2"))

    def test_override(self):
        p = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": "/h", "HARNESS2_HOME": "/custom"}, home="/h", release="6")
        self.assertEqual(str(p.state_dir), "/custom")

    def test_empty_legacy_directory_is_not_state(self):
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, ".harness2"))
            p = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": home}, home=home, release="6")
            self.assertEqual(str(p.state_dir), os.path.join(home, ".local", "state", "harness2"))

    def test_platform_info_does_not_retain_secrets(self):
        p = detect_platform(
            sys_platform="linux", os_name="posix",
            env={"HOME": "/h", "PATH": "/bin", "OPENAI_API_KEY": "secret", "OTHER_TOKEN": "secret2"},
            home="/h", release="6",
        )
        self.assertNotIn("OPENAI_API_KEY", p.env)
        self.assertNotIn("OTHER_TOKEN", p.env)
        self.assertNotIn("secret", repr(p))
        self.assertEqual(p.credentials["OPENAI_API_KEY"], "secret")

    def test_credentials_are_captured_from_explicit_platform_environment(self):
        with tempfile.TemporaryDirectory() as home:
            p = detect_platform(
                sys_platform="linux", os_name="posix",
                env={"HOME": home, "PATH": "", "OPENAI_API_KEY": "captured-value"},
                home=home, release="6",
            )
            config = HarnessConfig(platform=p, state_root=os.path.join(home, "state"))
            with patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-value"}):
                self.assertEqual(config.credential("OPENAI_API_KEY"), "captured-value")


class StateRootResolutionTests(unittest.TestCase):
    """Phase 10.1: deterministic state-root resolution (A-I cases).

    A valid Harness state root contains a readable kernel database carrying
    the kernel schema-migrations marker; directory existence alone never
    counts. Conflict between two valid roots is an explicit error, never a
    silent preference.
    """

    @staticmethod
    def valid_root(path):
        import sqlite3
        os.makedirs(path, exist_ok=True)
        con = sqlite3.connect(os.path.join(path, "harness.db"))
        con.execute(
            "CREATE TABLE kernel_schema_migrations (version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at REAL NOT NULL)"
        )
        con.execute(
            "INSERT INTO kernel_schema_migrations VALUES (5, 'harness_sessions', 'x', 1.0)"
        )
        con.commit()
        con.close()

    def _linux(self, home, **env):
        values = {"HOME": home}
        values.update(env)
        return detect_platform(
            sys_platform="linux", os_name="posix", env=values, home=home, release="6.8",
        )

    def test_a_canonical_valid_legacy_absent(self):
        with tempfile.TemporaryDirectory() as home:
            root = os.path.join(home, ".local", "state", "harness2")
            self.valid_root(root)
            self.assertEqual(str(self._linux(home).state_dir), root)

    def test_b_canonical_valid_legacy_empty_dir(self):
        with tempfile.TemporaryDirectory() as home:
            root = os.path.join(home, ".local", "state", "harness2")
            self.valid_root(root)
            os.mkdir(os.path.join(home, ".harness2"))
            self.assertEqual(str(self._linux(home).state_dir), root)

    def test_c_legacy_valid_canonical_absent(self):
        with tempfile.TemporaryDirectory() as home:
            legacy = os.path.join(home, ".harness2")
            self.valid_root(legacy)
            self.assertEqual(str(self._linux(home).state_dir), legacy)

    def test_d_both_valid_raises_split_state(self):
        from harness2.platforms import HarnessSplitStateError
        with tempfile.TemporaryDirectory() as home:
            self.valid_root(os.path.join(home, ".harness2"))
            self.valid_root(os.path.join(home, ".local", "state", "harness2"))
            with self.assertRaises(HarnessSplitStateError) as ctx:
                self._linux(home)
            message = str(ctx.exception)
            self.assertIn(".harness2", message)
            self.assertIn(".local/state/harness2", message)
            self.assertIn("HARNESS2_HOME", message)

    def test_e_override_wins_even_when_both_valid(self):
        from harness2.platforms import HarnessSplitStateError
        with tempfile.TemporaryDirectory() as home:
            self.valid_root(os.path.join(home, ".harness2"))
            self.valid_root(os.path.join(home, ".local", "state", "harness2"))
            p = self._linux(home, HARNESS2_HOME=os.path.join(home, "chosen"))
            self.assertEqual(str(p.state_dir), os.path.join(home, "chosen"))

    def test_f_fresh_linux_home_uses_xdg_canonical(self):
        with tempfile.TemporaryDirectory() as home:
            p = self._linux(home)
            self.assertEqual(str(p.state_dir), os.path.join(home, ".local", "state", "harness2"))

    def test_g_termux_legacy_is_canonical_no_conflict(self):
        import shutil
        with tempfile.TemporaryDirectory() as home:
            legacy = os.path.join(home, ".harness2")
            self.valid_root(legacy)
            p = detect_platform(
                sys_platform="linux", os_name="posix",
                env={"HOME": home, "PREFIX": "/data/data/com.termux/files/usr"},
                home=home, release="6",
            )
            self.assertEqual(str(p.state_dir), legacy)
            # An empty ~/.harness2 on Termux still resolves (Android canonical).
            shutil.rmtree(legacy)
            os.mkdir(legacy)
            p = detect_platform(
                sys_platform="linux", os_name="posix",
                env={"HOME": home, "PREFIX": "/data/data/com.termux/files/usr"},
                home=home, release="6",
            )
            self.assertEqual(str(p.state_dir), legacy)

    def test_h_windows_canonical_local_and_legacy_only_compat(self):
        with tempfile.TemporaryDirectory() as home:
            local = os.path.join(home, "AppData", "Local")
            os.makedirs(os.path.join(local, "Harness2"))
            self.valid_root(os.path.join(local, "Harness2"))
            p = detect_platform(
                sys_platform="win32", os_name="nt",
                env={"USERPROFILE": home, "LOCALAPPDATA": local, "APPDATA": home},
                home=home, release="10",
            )
            self.assertEqual(str(p.state_dir), os.path.join(local, "Harness2"))
            # Legacy-only Windows install keeps working (compat).
            legacy = os.path.join(home, ".harness2")
            self.valid_root(legacy)
            os.rename(os.path.join(local, "Harness2"), os.path.join(local, "Harness2.old"))
            p = detect_platform(
                sys_platform="win32", os_name="nt",
                env={"USERPROFILE": home, "LOCALAPPDATA": local, "APPDATA": home},
                home=home, release="10",
            )
            self.assertEqual(str(p.state_dir), legacy)

    def test_i_macos_canonical_application_support(self):
        with tempfile.TemporaryDirectory() as home:
            root = os.path.join(home, "Library", "Application Support", "Harness2")
            self.valid_root(root)
            p = detect_platform(
                sys_platform="darwin", os_name="posix", env={"HOME": home}, home=home, release="24",
            )
            self.assertEqual(str(p.state_dir), root)

    def test_unrelated_files_do_not_mark_state(self):
        with tempfile.TemporaryDirectory() as home:
            legacy = os.path.join(home, ".harness2")
            os.makedirs(legacy)
            with open(os.path.join(legacy, "random.log"), "w", encoding="utf-8") as stream:
                stream.write("not harness state")
            self.assertEqual(str(self._linux(home).state_dir),
                             os.path.join(home, ".local", "state", "harness2"))

    def test_conflict_reported_by_cli_main_with_exit_2(self):
        with tempfile.TemporaryDirectory() as home:
            self.valid_root(os.path.join(home, ".harness2"))
            self.valid_root(os.path.join(home, ".local", "state", "harness2"))
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                with patch("harness2.cli.sys.argv", ["harness", "status"]):
                    from harness2.cli import main
                    code = main()
            self.assertEqual(code, 2)


class LaunchTests(unittest.TestCase):
    def test_windows_cmd_prefix_and_flags(self):
        p = detect_platform(sys_platform="win32", os_name="nt", env={"USERPROFILE": "C:/U", "COMSPEC": "C:/Windows/cmd.exe"}, home="C:/U", release="10")
        self.assertEqual(p.command_prefix("C:/x/tool.cmd")[:5], ["C:/Windows/cmd.exe", "/d", "/s", "/c", "C:/x/tool.cmd"])
        self.assertIn("creationflags", p.background_kwargs())

    def test_windows_powershell_override_is_not_falsely_executable(self):
        with tempfile.TemporaryDirectory() as home:
            script = os.path.join(home, "provider.ps1")
            with open(script, "w", encoding="utf-8") as stream:
                stream.write("exit 0\n")
            p = detect_platform(
                sys_platform="win32", os_name="nt",
                env={"USERPROFILE": home, "PATH": ""}, home=home, release="10",
            )
            self.assertIsNone(p.executable(script))

    def test_posix_direct_and_session(self):
        p = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": "/h"}, home="/h", release="6")
        self.assertEqual(p.command_prefix("/bin/x"), ["/bin/x"])
        self.assertEqual(p.background_kwargs(), {"start_new_session": True})

    def test_discover_override_and_which(self):
        with tempfile.TemporaryDirectory() as home:
            executable = os.path.join(home, "node")
            with open(executable, "w", encoding="utf-8") as stream:
                stream.write("#!/bin/sh\nexit 0\n")
            os.chmod(executable, 0o700)
            p = detect_platform(
                sys_platform="linux", os_name="posix",
                env={"HOME": home, "HARNESS_NODE_BIN": executable, "PATH": ""},
                home=home, release="6",
            )
            self.assertEqual(p.discover("node"), os.path.realpath(executable))
            p2 = detect_platform(
                sys_platform="linux", os_name="posix",
                env={"HOME": home, "PATH": home}, home=home, release="6",
            )
            with patch("harness2.platforms.shutil.which", return_value=executable):
                self.assertEqual(p2.discover("node"), os.path.realpath(executable))

    def test_explicit_empty_path_does_not_search_host_environment(self):
        with tempfile.TemporaryDirectory() as home, patch(
            "harness2.platforms.shutil.which", return_value=None,
        ) as which:
            p = detect_platform(
                sys_platform="linux", os_name="posix",
                env={"HOME": home, "PATH": ""}, home=home, release="6",
            )
            self.assertIsNone(p.discover("missing-provider"))
            self.assertEqual(which.call_args.kwargs["path"], "")

    def test_config_prefers_running_interpreter_for_portable_install(self):
        p = detect_platform(
            sys_platform="linux", os_name="posix",
            env={"HOME": "/tmp/home", "PATH": "/usr/bin:/bin"},
            home="/tmp/home", release="linux",
        )
        config = HarnessConfig(platform=p, state_root="/tmp/harness-config-test")
        self.assertEqual(config.python_bin, os.path.abspath(sys.executable))

    def test_clean_environment_preserves_explicit_proot_contract(self):
        with tempfile.TemporaryDirectory() as home:
            state = os.path.join(home, "state")
            p = detect_platform(
                sys_platform="linux", os_name="posix",
                env={
                    "HOME": home,
                    "PATH": "/usr/bin:/bin",
                    "PREFIX": "/data/data/com.termux/files/usr",
                    "PROOT_DISTRO": "ubuntu",
                    "XDG_CONFIG_HOME": os.path.join(home, "config"),
                    "SSL_CERT_FILE": os.path.join(home, "ca.pem"),
                    "UNSAFE_INHERITED": "no",
                },
                home=home, release="6-PRoot-Distro",
            )
            config = HarnessConfig(platform=p, state_root=state)
            env = config.clean_env("local")
            self.assertEqual(env["PREFIX"], "/data/data/com.termux/files/usr")
            self.assertEqual(env["PROOT_DISTRO"], "ubuntu")
            self.assertEqual(env["XDG_CONFIG_HOME"], os.path.join(home, "config"))
            self.assertEqual(env["SSL_CERT_FILE"], os.path.join(home, "ca.pem"))
            self.assertEqual(env["TMPDIR"], os.path.join(state, "tmp"))
            self.assertNotIn("UNSAFE_INHERITED", env)
            self.assertEqual(len(config.execution_profile()["sha256"]), 64)


class UrlTests(unittest.TestCase):
    def test_loopback(self):
        for url in ("http://127.0.0.1:8080/v1", "http://localhost/x", "http://[::1]:80"):
            self.assertTrue(is_loopback_url(url))
        for url in ("http://0.0.0.0:8080", "http://192.168.1.2", "not a url"):
            self.assertFalse(is_loopback_url(url))


if __name__ == "__main__":
    unittest.main()
