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

    def test_existing_legacy_preferred(self):
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, ".harness2"))
            p = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": home}, home=home, release="6")
            self.assertEqual(str(p.state_dir), os.path.join(home, ".harness2"))

    def test_platform_info_does_not_retain_secrets(self):
        p = detect_platform(
            sys_platform="linux", os_name="posix",
            env={"HOME": "/h", "PATH": "/bin", "OPENAI_API_KEY": "secret", "OTHER_TOKEN": "secret2"},
            home="/h", release="6",
        )
        self.assertNotIn("OPENAI_API_KEY", p.env)
        self.assertNotIn("OTHER_TOKEN", p.env)
        self.assertNotIn("secret", repr(p))


class LaunchTests(unittest.TestCase):
    def test_windows_cmd_prefix_and_flags(self):
        p = detect_platform(sys_platform="win32", os_name="nt", env={"USERPROFILE": "C:/U", "COMSPEC": "C:/Windows/cmd.exe"}, home="C:/U", release="10")
        self.assertEqual(p.command_prefix("C:/x/tool.cmd")[:5], ["C:/Windows/cmd.exe", "/d", "/s", "/c", "C:/x/tool.cmd"])
        self.assertIn("creationflags", p.background_kwargs())

    def test_posix_direct_and_session(self):
        p = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": "/h"}, home="/h", release="6")
        self.assertEqual(p.command_prefix("/bin/x"), ["/bin/x"])
        self.assertEqual(p.background_kwargs(), {"start_new_session": True})

    def test_discover_override_and_which(self):
        p = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": "/h", "HARNESS_NODE_BIN": "/custom/node", "PATH": "/bin"}, home="/h", release="6")
        with patch("harness2.platforms._usable_executable", return_value=True):
            self.assertEqual(p.discover("node"), "/custom/node")
        p2 = detect_platform(sys_platform="linux", os_name="posix", env={"HOME": "/h", "PATH": "/bin"}, home="/h", release="6")
        with patch("harness2.platforms.shutil.which", return_value="/bin/node"):
            self.assertEqual(p2.discover("node"), "/bin/node")

    def test_config_prefers_running_interpreter_for_portable_install(self):
        p = detect_platform(
            sys_platform="linux", os_name="posix",
            env={"HOME": "/tmp/home", "PATH": "/usr/bin:/bin"},
            home="/tmp/home", release="linux",
        )
        config = HarnessConfig(platform=p, state_root="/tmp/harness-config-test")
        self.assertEqual(config.python_bin, os.path.abspath(sys.executable))


class UrlTests(unittest.TestCase):
    def test_loopback(self):
        for url in ("http://127.0.0.1:8080/v1", "http://localhost/x", "http://[::1]:80"):
            self.assertTrue(is_loopback_url(url))
        for url in ("http://0.0.0.0:8080", "http://192.168.1.2", "not a url"):
            self.assertFalse(is_loopback_url(url))


if __name__ == "__main__":
    unittest.main()
