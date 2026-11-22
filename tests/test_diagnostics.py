"""Exercise opt-in recommendations and private installation audit records."""

import unittest

import test_installer_binary as binary_support
import test_libponnet as lib_support
from paths import BUSYBOX, DIST


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class DiagnosticsTests(unittest.TestCase):
    """Verify diagnostic decisions never silently authorize device changes."""

    def setUp(self) -> None:
        """Create a known binary and module state with patching disabled."""
        self.binary = binary_support.InstallerBinaryTests("test_native_uni_skips_binary_and_uses_live_preflight")
        self.binary.setUp()
        self.helper = self.binary.helper
        self.module = self.binary.module
        self.helper.config.write_text("schema_version=1\n")

    def tearDown(self) -> None:
        """Remove all synthetic module files."""
        self.binary.tearDown()

    def make_native_live(self) -> None:
        """Select the working native profile and leave firmware failsafe."""
        self.binary.mib.write_text("11 0x0101 0 0\n")
        self.binary.failsafe.unlink(missing_ok=True)

    def write_omci_log(self, text: str) -> None:
        """Create a synthetic module log with controlled evidence."""
        log = self.module / "tmp/omcid.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text)

    def assert_unmodified_setup(self) -> None:
        """Require that a rejected installation preserves setup and library state."""
        self.helper.assert_runtime_absent()
        self.assertEqual(lib_support.STOCK_BYTES, self.binary.library.read_bytes())
        self.assertFalse((self.helper.conf / "dei835-originals").exists())
        self.assertFalse((self.helper.conf / "dei-pon.toml").exists())
        self.assertFalse((self.helper.conf / "dei835-libponnet.stock").exists())
        self.assertFalse((self.helper.conf / "setup-state/fwenv.originals").exists())
        self.assertFalse(self.helper.fixture.run_dir.exists())

    def test_check_reports_patch_and_gems_without_creating_persistent_log(self) -> None:
        """Keep diagnostic checks read-only even when a workaround is suggested."""
        result = self.helper.invoke("check")
        self.helper.success(result)
        self.assertIn("enabled = true", result.stdout)
        self.assertIn("GEM", result.stdout)
        self.assertFalse((self.helper.conf / "install.log").exists())
        self.assert_unmodified_setup()

    def test_required_patch_without_opt_in_stops_before_configuration_writes(self) -> None:
        """Save the recommendation but refuse setup changes without opt-in."""
        result = self.helper.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("enabled = true", result.stdout)
        self.assertIn("rerun", result.stdout)
        self.assert_unmodified_setup()
        log = self.helper.conf / "install.log"
        content = log.read_text()
        self.assertIn("Binary patch recommendation: suggested", content)
        self.assertIn("result=failed phase=patch-opt-in-required", content)
        self.assertEqual(0o600, log.stat().st_mode & 0o777)

    def test_policy_auto_without_enabled_is_not_authorization(self) -> None:
        """Require the new opt-in even when an older configuration selected auto."""
        self.helper.config.write_text('schema_version=1\n[library_patch]\npolicy="auto"\n')
        result = self.helper.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assert_unmodified_setup()

    def test_direct_patch_action_cannot_bypass_config_opt_in(self) -> None:
        """Keep the standalone patch action subject to explicit configuration."""
        result = self.helper.invoke("patch-library")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("enabled = true", result.stdout + result.stderr)
        self.assert_unmodified_setup()

    def test_o5_without_specific_errors_does_not_recommend_binary_patch(self) -> None:
        """Do not confuse successful PON activation with a UNI mapping failure."""
        self.make_native_live()
        self.write_omci_log("MIB reset failed\nGEM key_errors=42\n")
        result = self.helper.invoke("install")
        self.helper.success(result)
        self.assertIn("PON context: O5", result.stdout)
        self.assertIn("Binary patch recommendation: not-indicated", result.stdout)
        self.assertEqual(lib_support.STOCK_BYTES, self.binary.library.read_bytes())

    def test_specific_log_error_is_advisory_and_saved_without_raw_lines(self) -> None:
        """Record canonical log evidence while keeping private payloads out of logs."""
        self.make_native_live()
        self.write_omci_log(
            "pon_net_ifname_get() failed with -12 PRIVATE_LOG_VALUE\n"
            'Can\'t create Managed Entity "PPTP Ethernet UNI" 11@517 PRIVATE_LOG_VALUE\n'
        )
        result = self.helper.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("entries may be historical", result.stdout)
        self.assertIn("enabled = true", result.stdout)
        self.assert_unmodified_setup()
        content = (self.helper.conf / "install.log").read_text()
        self.assertIn("UNI-517-creation-errors=1", content)
        self.assertNotIn("PRIVATE_LOG_VALUE", content)
        self.assertNotIn("PRIVATE_LOG_VALUE", result.stdout + result.stderr)

    def test_healthy_install_records_complete_selected_gem_set(self) -> None:
        """Persist the selected service mapping even when no repair is needed."""
        self.make_native_live()
        result = self.helper.invoke("install")
        self.helper.success(result)
        content = (self.helper.conf / "install.log").read_text()
        self.assertIn("GEMs=1084 1085 1086 1087", result.stdout)
        self.assertIn("GEMs=1084 1085 1086 1087", content)
        self.assertIn("mapper=pmapper1", content)
        self.assertIn("result=completed", content)
        self.assertNotIn("ZTEG1234ABCD", content)

    def test_log_evidence_and_opt_in_do_not_override_native_mib_eligibility(self) -> None:
        """Keep a native library unchanged when only advisory log evidence matches."""
        self.make_native_live()
        self.helper.config.write_text("schema_version=1\n[library_patch]\nenabled=true\n")
        self.write_omci_log('Can\'t create Managed Entity "PPTP Ethernet UNI" 11@517\n')
        result = self.helper.invoke("install")
        self.helper.success(result)
        self.assertIn("Log evidence is advisory", result.stdout)
        self.assertEqual(lib_support.STOCK_BYTES, self.binary.library.read_bytes())
        self.assertFalse((self.helper.conf / "dei835-libponnet.stock").exists())
        self.assertFalse((self.helper.conf / "dei835-libponnet.owner").exists())

    def test_installed_patch_needs_no_new_opt_in(self) -> None:
        """Do not ask to reapply a verified patch because old log errors remain."""
        self.binary.library.write_bytes(lib_support.PATCHED_BYTES)
        self.write_omci_log('Can\'t create Managed Entity "PPTP Ethernet UNI" 11@517\n')
        result = self.helper.invoke("install")
        self.helper.success(result)
        self.assertIn("already patched", result.stdout)
        self.assertNotIn("Binary patch requires explicit opt-in", result.stderr)
        self.assertEqual(lib_support.PATCHED_BYTES, self.binary.library.read_bytes())

    def test_audit_rotation_preserves_previous_log_privately(self) -> None:
        """Rotate a large recognized audit while retaining its prior contents."""
        self.make_native_live()
        self.helper.success(self.helper.invoke("install"))
        log = self.helper.conf / "install.log"
        previous = "DEI-PON-INSTALL-AUDIT-v1\n" + "old audit record\n" * 9000
        log.write_text(previous)
        self.helper.success(self.helper.invoke("install"))
        old = self.helper.conf / "install.log.old"
        self.assertEqual(previous, old.read_text())
        self.assertEqual(0o600, old.stat().st_mode & 0o777)
        self.assertIn("GEMs=1084 1085 1086 1087", log.read_text())

    def test_audit_symlink_is_not_followed(self) -> None:
        """Refuse an audit symlink without writing its external target."""
        self.make_native_live()
        target = self.helper.fixture.root / "private-data"
        target.write_text("PRIVATE_LOG_VALUE\n")
        (self.helper.conf / "install.log").symlink_to(target)
        result = self.helper.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("PRIVATE_LOG_VALUE\n", target.read_text())
        self.assert_unmodified_setup()

    def test_unrecognized_audit_is_preserved(self) -> None:
        """Preserve unrelated files instead of claiming them as installation logs."""
        self.make_native_live()
        log = self.helper.conf / "install.log"
        log.write_text("PRIVATE_LOG_VALUE\n")
        result = self.helper.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("PRIVATE_LOG_VALUE\n", log.read_text())
        self.assertNotIn("PRIVATE_LOG_VALUE", result.stdout + result.stderr)
        self.assert_unmodified_setup()

    def test_completion_audit_failure_is_not_reported_as_success(self) -> None:
        """Return failure when completion cannot be logged after a successful setup."""
        self.make_native_live()
        script = (DIST / "dei-pon-install.sh").read_text()
        marker = 'echo "Saved configuration: ${D}/dei-pon.toml"'
        self.assertIn(marker, script)
        script = script.replace(marker, "install_audit_finish() { return 1; }\n" + marker)
        altered = self.helper.fixture.root / "audit-failure.sh"
        altered.write_text(script)
        result = self.helper.invoke("install", installer=altered)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("audit completion could not be saved", result.stderr)
        self.assertTrue((self.helper.conf / "dei-pon.toml").exists())
        self.assertNotIn("result=completed", (self.helper.conf / "install.log").read_text())


if __name__ == "__main__":
    unittest.main()
