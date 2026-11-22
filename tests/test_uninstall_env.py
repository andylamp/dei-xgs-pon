"""Exercise firmware undo ownership and interrupted writes on a synthetic module."""

import unittest
from typing import TYPE_CHECKING

import test_setup_engine as support
from paths import BUSYBOX, SOURCE

if TYPE_CHECKING:
    import subprocess


@unittest.skipUnless(BUSYBOX, "BusyBox is required for firmware undo tests")
class UninstallEnvTests(unittest.TestCase):
    """Restore only firmware settings changed by this installer."""

    def setUp(self) -> None:
        """Create a native stock module with both absent and present-empty values."""
        self.helper = support.SetupTests("test_check_is_read_only_and_hides_serial")
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.helper.fresh()
        self.helper.config["CFG_ONU_MIB_PROFILE"] = "native_1u"
        self.helper.config["CFG_MANAGEMENT_PING_IP"] = "192.0.2.9"
        self.helper.env_values.update({"8311_ping_ip": "", "8311_fix_vlans": "2"})
        self.helper.save_env()
        self.helper.put("ptconf/8311/dei-pon.toml", 'schema_version=1\n[onu]\nmib_profile="native_1u"\n')
        self.backup = self.helper.root / "ptconf/8311/setup-state/fwenv.originals"
        self.journal = self.backup / "applied-v1"
        self.staging = self.helper.root / "staging"
        self.staging.mkdir()

    def run_undo(self, action: str = "uninstall_env_apply") -> subprocess.CompletedProcess[str]:
        """Source the verified libraries and run firmware restoration.

        Returns:
            Captured status and sanitized messages from BusyBox.

        """
        script = (
            f'S="{self.staging}"\n. "{SOURCE / "dei-config.sh"}" || exit 1\n'
            f'. "{SOURCE / "uninstall-env.sh"}" || exit 1\n{action}\nexit $?'
        )
        return self.helper.run_engine(script)

    def test_fresh_native_restore_distinguishes_empty_absent_and_unmanaged_values(self) -> None:
        """Remove a freshly supplied serial and preserve a subsequent unrelated change."""
        before = self.helper.current_env()
        install = self.helper.run_engine()
        self.assertEqual(0, install.returncode, install.stderr)
        self.assertTrue((self.journal / "8311_gpon_sn").is_file())
        self.assertFalse((self.journal / "8311_fix_vlans").exists())
        self.helper.env_values = {**self.helper.current_env(), "8311_fix_vlans": "1"}
        self.helper.save_env()
        undo = self.run_undo()
        self.assertEqual(0, undo.returncode, undo.stderr)
        self.assertEqual({**before, "8311_fix_vlans": "1"}, self.helper.current_env())
        self.assertNotIn("ZTEGABCD1234", undo.stdout + undo.stderr)
        self.assertNotIn("192.0.2.9", undo.stdout + undo.stderr)

    def test_failed_reinstall_keeps_last_verified_receipt_for_undo(self) -> None:
        """Accept the prior installed value after the next intended write fails."""
        before = self.helper.current_env()
        self.assertEqual(0, self.helper.run_engine().returncode)
        self.helper.config["CFG_MANAGEMENT_FAILSAFE_DELAY"] = "42"
        result = self.helper.run_engine(FAIL_WRITE="8311_failsafe_delay")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("42\n", (self.journal / "8311_failsafe_delay.pending").read_text())
        self.assertEqual("30\n", (self.journal / "8311_failsafe_delay").read_text())
        undo = self.run_undo()
        self.assertEqual(0, undo.returncode, undo.stderr)
        self.assertEqual(before, self.helper.current_env())

    def test_edited_saved_profile_cannot_disguise_non_native_mib_receipt(self) -> None:
        """Reject other journaled selector values even when the TOML says native."""
        self.assertEqual(0, self.helper.run_engine().returncode)
        (self.journal / "mib_file").write_text("/ptconf/8311/dei.ini\n")
        before = self.helper.current_env()
        undo = self.run_undo()
        self.assertNotEqual(0, undo.returncode)
        self.assertIn("non-native MIB", undo.stderr)
        self.assertEqual(before, self.helper.current_env())

    def test_before_image_without_config_or_writes_is_a_safe_partial_install(self) -> None:
        """Recover a first installation interrupted before its config was saved."""
        self.assertEqual(0, self.helper.run_engine("setup_paths; setup_backup || exit 1; exit 0").returncode)
        (self.helper.root / "ptconf/8311/dei-pon.toml").unlink()
        before = self.helper.current_env()
        undo = self.run_undo()
        self.assertEqual(0, undo.returncode, undo.stderr)
        self.assertEqual(before, self.helper.current_env())
