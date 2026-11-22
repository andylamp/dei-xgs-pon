"""Exercise the standalone one-time cleanup against synthetic module files."""

import fcntl
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from paths import BUSYBOX, FIXTURES, ROOT

GUIDE_SHA256 = {
    "vlan_fixes_hook.sh": "a0a56570e02db55880888fa0aac85bf6a26aa11060a840cd2ea212f69a02366b",
    "keyring.sh": "af46d3f383b7262e9f7fe4e6718973c2a036fa49e0ea9eb2fb0bff7615eebba7",
    "status.sh": "1f362bcedcacf0ed44a7e5318b2c530c521d18be64dea99c589d94f07a92dca8",
}


@unittest.skipUnless(BUSYBOX, "The cleanup runs under BusyBox ash")
class RemoveGuideScriptsTests(unittest.TestCase):
    """Check verified deletion, all-target preflight, and the firmware interlock."""

    def setUp(self) -> None:
        """Substitute known fixture digests only in a private copy of the cleanup."""
        assert BUSYBOX is not None
        self.busybox = BUSYBOX
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.conf = self.root / "ptconf/8311"
        self.conf.mkdir(parents=True)
        self.temp = self.root / "tmp"
        self.temp.mkdir()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.script = self.root / "remove-guide-scripts.sh"
        source = (ROOT / "tools/remove-guide-scripts.sh").read_text()
        for name, published in GUIDE_SHA256.items():
            data = (FIXTURES / "guide" / name).read_bytes()
            (self.conf / name).write_bytes(data)
            self.assertEqual(source.count(published), 1)
            source = source.replace(published, hashlib.sha256(data).hexdigest())
        self.script.write_text(source)

    def invoke(self, *args: str, shell_functions: str = "") -> subprocess.CompletedProcess[str]:
        """Run the isolated tool with an empty authentication environment.

        Returns:
            Exit status and captured console output.

        """
        return subprocess.run(
            [
                self.busybox,
                "ash",
                "-c",
                shell_functions + '\nscript=$1; shift; . "$script"',
                "test",
                self.script,
                *args,
            ],
            env={"PATH": os.defpath, "DEI835_MODULE_ROOT": str(self.root)},
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )

    def test_preview_preserves_all_files_and_never_creates_a_lock(self) -> None:
        """The default and explicit previews name every target without writing."""
        for args in ((), ("--dry-run",)):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in GUIDE_SHA256:
                    self.assertIn(f"Would remove {self.conf / name}", result.stdout)
                    self.assertEqual((self.conf / name).read_bytes(), (FIXTURES / "guide" / name).read_bytes())
                self.assertEqual(list(self.temp.iterdir()), [])
        self.assertEqual(self.invoke("--apply", "--dry-run").returncode, 2)

    def test_apply_removes_only_the_three_verified_active_files_in_hook_first_order(self) -> None:
        """Retire the startup path first and leave data elsewhere intact on later runs."""
        backup = self.conf / "dei835-originals"
        backup.mkdir()
        private_config = backup / "dei-pon.toml.before"
        private_config.write_text("operator configuration\n")
        saved = self.conf / "dei-pon.toml"
        saved.write_text("operator selection\n")
        result = self.invoke("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"Removed {self.conf / name}" for name in GUIDE_SHA256])
        self.assertEqual({backup, saved}, set(self.conf.iterdir()))
        self.assertEqual(private_config.read_text(), "operator configuration\n")
        after = self.invoke("--apply")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn("No active guide scripts", after.stdout)

    def test_partial_legacy_install_and_missing_directory(self) -> None:
        """Remove the remaining recognized script and allow a fully clean stick."""
        for name in ("vlan_fixes_hook.sh", "keyring.sh"):
            (self.conf / name).unlink()
        result = self.invoke("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"Removed {self.conf / 'status.sh'}")
        self.conf.rmdir()
        self.assertEqual(self.invoke("--apply").returncode, 0)
        self.assertFalse(self.conf.exists())

    def test_unknown_project_or_special_file_blocks_removal_before_the_hook_is_touched(self) -> None:
        """An edited file, current project script, symlink or FIFO invalidates the whole plan."""
        candidate = self.conf / "status.sh"
        good = candidate.read_bytes()
        external = self.root / "verified-elsewhere"
        external.write_bytes(good)
        for kind in ("edited", "project", "symlink", "dangling", "fifo"):
            with self.subTest(kind=kind):
                candidate.unlink()
                if kind == "edited":
                    candidate.write_bytes(good + b"# operator edits\n")
                elif kind == "project":
                    candidate.write_bytes((ROOT / "src/status.sh").read_bytes())
                elif kind == "fifo":
                    os.mkfifo(candidate)
                else:
                    candidate.symlink_to(external if kind == "symlink" else self.root / "absent")
                result = self.invoke("--apply")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("status.sh", result.stderr)
                self.assertTrue((self.conf / "vlan_fixes_hook.sh").is_file())
                self.assertTrue((self.conf / "keyring.sh").is_file())
                self.assertEqual(external.read_bytes(), good)

    def test_unknown_lct_or_mixed_deployment_preserves_guide_files(self) -> None:
        """Require manual review for an unsupported helper or a partial project install."""
        for name in ("lctfix.sh", "dei-835-lib.sh", "dei-config.sh"):
            with self.subTest(name=name):
                flag = self.conf / name
                flag.write_text("operator file\n")
                result = self.invoke("--apply")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertTrue(all((self.conf / target).is_file() for target in GUIDE_SHA256))
                self.assertEqual(flag.read_text(), "operator file\n")
                flag.unlink()

    def test_busy_firmware_hook_and_active_detached_helper_defer_removal(self) -> None:
        """Wait for firmware and its detached helper even when the helper has no PID file."""
        with (self.temp / "8311-fix-vlans.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.invoke("--apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("busy", result.stderr)
        pid = self.proc / "100"
        pid.mkdir()
        (pid / "cmdline").write_bytes(b"/bin/sh\x00/ptconf/8311/keyring.sh\x00")
        result = self.invoke("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active", result.stderr)
        self.assertTrue(all((self.conf / target).is_file() for target in GUIDE_SHA256))

    def test_valid_digest_from_an_unsuccessful_command_cannot_authorize_removal(self) -> None:
        """Require sha256sum to report a successful file read as well as a matching digest."""
        result = self.invoke("--apply", shell_functions='sha256sum() { command sha256sum "$@"; return 1; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot verify", result.stderr)
        self.assertTrue(all((self.conf / target).is_file() for target in GUIDE_SHA256))
