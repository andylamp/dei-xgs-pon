"""Configuration selection, clean setup and migration integration tests."""

import shutil
import unittest
from typing import TYPE_CHECKING

import test_installer as support
import test_persistence as runtime_support
from paths import BUSYBOX, FIXTURES

if TYPE_CHECKING:
    from pathlib import Path
    from subprocess import CompletedProcess


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class ConfigInstallerTests(unittest.TestCase):
    """Verify configuration selection and compatibility across installer upgrades."""

    def setUp(self) -> None:
        """Reuse the isolated installer fixture for configuration-selection tests."""
        self.helper = support.InstallerTests("test_check_does_not_install_or_modify_configuration")
        self.helper.setUp()
        self.fixture = self.helper.fixture
        self.conf = self.helper.conf

    def tearDown(self) -> None:
        """Remove the temporary files created for this test."""
        self.helper.tearDown()

    def invoke(
        self,
        action: str,
        installer: Path | None = None,
        *,
        extra: tuple[str, ...] = (),
        allow_fw: bool = False,
    ) -> CompletedProcess[str]:
        """Run an installer action and verify its permitted mock mutations.

        Returns:
            The captured installer process result.

        """
        return self.helper.invoke(action, installer, extra=extra, allow_fw=allow_fw)

    def test_explicit_missing_file_never_falls_back_to_saved_config(self) -> None:
        """Reject a missing explicit configuration despite a saved alternative."""
        (self.conf / "dei-pon.toml").write_text("schema_version=1\n")
        result = self.invoke("check", extra=("--config", str(self.fixture.root / "missing.toml")))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Configuration must be a readable regular file", result.stderr)
        self.helper.assert_runtime_absent()

    def test_empty_and_repeated_config_arguments_are_rejected(self) -> None:
        """Reject empty or repeated configuration selections."""
        for args in (("--config", ""), ("--config", str(self.helper.config), "--config", str(self.helper.config))):
            result = self.invoke("check", extra=args)
            self.assertNotEqual(0, result.returncode)
        self.helper.env["DEI835_CONFIG_PATH"] = str(self.helper.config)
        self.helper.env["DEI_PON_CONFIG_PATH"] = ""
        self.assertNotEqual(0, self.invoke("check").returncode)

    def test_check_selection_does_not_replace_saved_config(self) -> None:
        """Inspect a selected configuration without replacing the saved one."""
        saved = "schema_version=1\n[internet]\nvlan=835\n"
        (self.conf / "dei-pon.toml").write_text(saved)
        selected = self.fixture.root / "selected.toml"
        selected.write_text("schema_version=1\n[internet]\nvlan=836\n")
        result = self.invoke("check", extra=("--config", str(selected)))
        self.helper.success(result)
        self.assertIn("Selected WAN VLAN: 836", result.stdout)
        self.assertIn("VLAN 836: mapper=pmapper2", result.stdout)
        self.assertEqual(saved, (self.conf / "dei-pon.toml").read_text())
        self.helper.assert_runtime_absent()

    def test_install_commits_complete_private_config_and_switches_saved_selection(self) -> None:
        """Save the selected complete configuration with private permissions."""
        self.helper.success(self.invoke("install"))
        selected = self.fixture.root / "836.toml"
        selected.write_text("schema_version=1\n[internet]\nvlan=836\n")
        result = self.invoke("install", extra=("--config", str(selected)))
        self.helper.success(result)
        self.assertEqual(selected.read_text(), (self.conf / "dei-pon.toml").read_text())
        self.assertEqual(0o600, (self.conf / "dei-pon.toml").stat().st_mode & 0o777)
        previous_config = self.conf / "setup-state/dei-pon.toml.before"
        self.assertEqual(self.helper.config.read_bytes(), previous_config.read_bytes())
        self.assertEqual(0o600, previous_config.stat().st_mode & 0o777)
        del self.helper.env["DEI_PON_CONFIG_PATH"]
        result = self.invoke("check")
        self.helper.success(result)
        self.assertIn("Selected WAN VLAN: 836", result.stdout)

    def test_canonical_environment_wins_and_legacy_selection_still_works(self) -> None:
        """Prefer the neutral selector and continue to read older explicit selections."""
        selected = self.fixture.root / "another service.toml"
        selected.write_text("schema_version=1\n[internet]\nvlan=836\n")
        self.helper.env["DEI835_CONFIG_PATH"] = str(selected)
        result = self.invoke("check")
        self.helper.success(result)
        self.assertIn("VLAN 835: mapper=pmapper1", result.stdout)
        del self.helper.env["DEI_PON_CONFIG_PATH"]
        result = self.invoke("check")
        self.helper.success(result)
        self.assertIn("VLAN 836: mapper=pmapper2", result.stdout)

    def test_invalid_config_does_not_change_scripts_or_create_setup_backup(self) -> None:
        """Reject invalid configuration before creating installation state."""
        self.helper.config.write_text("schema_version=1\n[internet]\nvlan=837\n")
        result = self.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.helper.assert_runtime_absent()
        self.assertFalse((self.conf / "setup-state").exists())

    def test_historical_project_runtimes_upgrade_and_rollback_to_absence(self) -> None:
        """Upgrade every distributed project runtime on a clean baseline."""
        versions = (
            "v1",
            "v0.1.0",
            "v0.1.0-toolchain",
            "v0.1.0-diagnostics",
            "v0.1.0-audit",
            "v0.1.0-vanilla",
            "v0.1.0-selector",
        )
        for version in versions:
            with self.subTest(version=version):
                for previous in (FIXTURES / version).glob("*.sh"):
                    shutil.copyfile(previous, self.conf / previous.name)
                self.helper.success(self.invoke("install"))
                for name in support.RUNTIME:
                    self.assertEqual((runtime_support.SOURCE / name).read_bytes(), (self.conf / name).read_bytes())
                self.helper.success(self.invoke("rollback"))
                self.helper.assert_runtime_absent()

    def test_modified_previous_version_is_not_accepted_as_known_hash(self) -> None:
        """Reject edited historical payloads instead of treating them as recognized."""
        previous = FIXTURES / "v0.1.0"
        for path in previous.glob("*.sh"):
            shutil.copyfile(path, self.conf / path.name)
        edited = self.conf / "keyring.sh"
        contents = edited.read_bytes() + b"\n# retained local customization\n"
        edited.write_bytes(contents)

        result = self.invoke("install")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual(contents, edited.read_bytes())

    def test_formatted_runtime_upgrades_without_implicitly_enabling_patch(self) -> None:
        """Recognize the latest shipped runtime while retaining default patch opt-out."""
        for previous in (FIXTURES / "v0.1.0-toolchain").glob("*.sh"):
            shutil.copyfile(previous, self.conf / previous.name)
        result = self.invoke("install")
        self.helper.success(result)
        self.assertIn("library patch enabled: false", result.stdout)
        self.assertIn("GEMs=1084 1085 1086 1087", (self.conf / "install.log").read_text())
        self.assertEqual((runtime_support.SOURCE / "keyring.sh").read_bytes(), (self.conf / "keyring.sh").read_bytes())

    def test_fresh_native_setup_is_phased_and_rollback_restores_absence(self) -> None:
        """Stage fresh native setup and restore initially absent runtime files."""
        native = self.helper.module / "etc/mibs/prx300_1U.ini"
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text("11 0x0101 0 0\n")
        self.fixture.state["fwenv"] = {}
        self.fixture.state["fix_mode"] = 0
        self.fixture.state["pon_state"] = 11
        self.helper.config.write_text('schema_version=1\n[onu]\nserial="ZTEG1234ABCD"\nmib_profile="native_1u"\n')
        self.helper.success(self.invoke("check"))
        result = self.invoke("install", allow_fw=True)
        self.helper.success(result)
        self.assertIn("planned reboot is required", result.stdout)
        self.assertEqual("ZTEG1234ABCD", self.fixture.state["fwenv"]["8311_gpon_sn"])
        self.assertEqual("/etc/mibs/prx300_1U.ini", self.fixture.state["fwenv"]["mib_file"])
        self.assertFalse((self.conf / "dei835-originals").exists())
        self.assertFalse((self.helper.module / "ptconf/.failsafe").exists())
        self.fixture.state["mutations"] = []
        self.helper.success(self.invoke("rollback"))
        for name in ("keyring.sh", "status.sh", "vlan_fixes_hook.sh"):
            self.assertFalse((self.conf / name).exists())
        self.assertTrue((self.conf / "setup-state/fwenv.originals").exists())

    def test_missing_serial_on_fresh_setup_is_reported_before_any_installation(self) -> None:
        """Require a fresh ONU identity before installing configuration or scripts."""
        self.fixture.state["fwenv"].pop("8311_gpon_sn")
        result = self.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Provide the provisioned ONU serial", result.stderr)
        self.helper.assert_runtime_absent()
        self.assertFalse((self.conf / "dei-pon.toml").exists())


if __name__ == "__main__":
    unittest.main()
