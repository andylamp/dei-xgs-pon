"""Exercise clean removal through the generated installer on an offline module."""

import fcntl
import unittest
from typing import TYPE_CHECKING

import test_installer as installer_support
import test_libponnet as binary_support
from paths import BUSYBOX

if TYPE_CHECKING:
    from pathlib import Path

RUNTIME = installer_support.RUNTIME
MANAGED_SETTINGS = {"8311_failsafe_delay", "8311_mib_file", "mib_file"}
NATIVE = "/etc/mibs/prx300_1U.ini"
CANDIDATE = """schema_version = 1
[onu]
mib_profile = "native_1u"
[management]
failsafe_delay = 15
[library_patch]
enabled = false
policy = "never"
[storage]
persistent_root = "preserve"
"""


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class UninstallTests(unittest.TestCase):
    """Verify a no-script baseline, ownership checks and repeatable recovery."""

    def setUp(self) -> None:
        """Start with empty runtime paths, stock bytes, and an implicit native MIB."""
        self.helper = installer_support.InstallerTests("test_check_does_not_install_or_modify_configuration")
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.conf = self.helper.conf
        self.fixture = self.helper.fixture
        self.helper.config.write_text(CANDIDATE)
        self.native = self.helper.module / NATIVE.removeprefix("/")
        self.native.parent.mkdir(parents=True, exist_ok=True)
        self.native.write_text("11 0x0101 0 0\n")
        self.library = self.helper.module / "usr/lib/libponnet.so.0.0.0"
        self.library.write_bytes(binary_support.STOCK_BYTES)
        stock = self.fixture.root / "stock-image"
        stock.write_bytes(binary_support.STOCK_BYTES)
        self.helper.env["LIBPATCH_STOCK_FIXTURE"] = str(stock)
        for key in MANAGED_SETTINGS:
            self.fixture.state["fwenv"].pop(key, None)
        self.original_env = self.fixture.state["fwenv"].copy()
        self.unrelated = self.conf / "operator-helper.sh"
        self.unrelated.write_text("keep my local helper\n")

    def install_candidate(self) -> None:
        """Install the native migration and check which firmware settings actually changed."""
        self.helper.success(self.helper.invoke("install", allow_fw=True))
        installed = self.fixture.state["fwenv"]
        self.assertEqual(
            {key for key, value in installed.items() if self.original_env.get(key) != value}, MANAGED_SETTINGS
        )
        self.fixture.state["mutations"] = []

    def assert_clean_active_paths(self) -> None:
        """Require clean runtime paths and unchanged unrelated files, MIB and library."""
        for name in RUNTIME:
            self.assertFalse((self.conf / name).exists(), name)
        self.assertEqual("keep my local helper\n", self.unrelated.read_text())
        self.assertEqual("11 0x0101 0 0\n", self.native.read_text())
        self.assertEqual(binary_support.STOCK_BYTES, self.library.read_bytes())

    def active_snapshot(self) -> dict[str, bytes]:
        """Capture active files before a simulated refusal.

        Returns:
            The exact bytes of each existing runtime script.

        """
        return {name: (self.conf / name).read_bytes() for name in RUNTIME if (self.conf / name).is_file()}

    def assert_project_removal(self) -> Path:
        """Verify removal and retain the recovery archive across an idempotent retry.

        Returns:
            The private recovery directory.

        """
        self.helper.success(self.helper.invoke("uninstall", allow_fw=True))
        self.assert_clean_active_paths()
        self.assertEqual(self.original_env, self.fixture.state["fwenv"])
        self.assertEqual(
            {("fwenv_set", key, None) for key in MANAGED_SETTINGS},
            {(m["kind"], m["key"], m["value"]) for m in self.fixture.state["mutations"] if m["kind"] == "fwenv_set"},
        )
        self.assertEqual(2, self.fixture.state.get("fix_mode", 2))
        for name in ("setup-state", "dei-pon.toml"):
            self.assertFalse((self.conf / name).exists(), name)
        archives = sorted(self.conf.glob("dei-uninstalled.*"))
        self.assertEqual(1, len(archives))
        archive = archives[0]
        self.assertEqual(0o700, archive.stat().st_mode & 0o777)
        self.assertEqual(CANDIDATE, (archive / "dei-pon.toml").read_text())
        self.fixture.state["mutations"] = []
        self.helper.success(self.helper.invoke("uninstall"))
        self.assertEqual(archives, sorted(self.conf.glob("dei-uninstalled.*")))
        self.assert_clean_active_paths()
        return archive

    def test_clean_install_uninstalls_to_absence_and_can_be_reinstalled(self) -> None:
        """Restore a vanilla runtime baseline and use fresh records on reinstall."""
        self.install_candidate()
        archive = self.assert_project_removal()
        self.assertFalse((archive / "dei835-originals").exists())
        self.install_candidate()
        self.helper.success(self.helper.invoke("rollback"))
        self.assert_clean_active_paths()

    def test_obsolete_backup_stays_inactive_across_install_and_removal(self) -> None:
        """An unrelated old backup cannot supply active scripts after recovery."""
        old_dir = self.conf / "dei835-originals"
        old_dir.mkdir()
        old_hook = old_dir / "vlan_fixes_hook.sh"
        content = b"# old hook kept for manual reference\n"
        old_hook.write_bytes(content)
        self.install_candidate()
        self.helper.success(self.helper.invoke("rollback"))
        self.assert_clean_active_paths()
        self.assertEqual(content, old_hook.read_bytes())
        archive = self.assert_project_removal()
        self.assertFalse((archive / old_dir.name).exists())
        self.assertEqual(content, old_hook.read_bytes())

    def test_absent_runtime_directory_is_an_idempotent_noop(self) -> None:
        """Skip device reads and avoid creating a config directory on a bare module."""
        self.conf.rename(self.fixture.root / "inactive-files")
        for extra in (("--dry-run",), ()):
            with self.subTest(extra=extra):
                self.helper.success(self.helper.invoke("uninstall", extra=extra))
                self.assertFalse(self.conf.exists())
                self.assertFalse([call for call in self.fixture.state["calls"] if call[0] == "fw_printenv"])

    def test_uncertain_original_fwenv_blocks_install_before_replacing_runtime(self) -> None:
        """Avoid creating an unrecoverable baseline when firmware storage cannot be read."""
        self.helper.proxies += (
            '\nfw_printenv() { if [ "$2" = 8311_ping_ip ]; then '
            'echo "I/O error" >&2; return 1; fi; "$DEI835_MOCK_BIN/fw_printenv" "$@"; }\n'
        )
        before = self.active_snapshot()
        result = self.helper.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(self.original_env, self.fixture.state["fwenv"])
        self.assertEqual(before, self.active_snapshot())
        self.assertFalse((self.conf / "setup-state/fwenv.originals").exists())

    def test_preview_of_native_migration_does_not_change_state(self) -> None:
        """Review prospective removals and firmware changes while retaining working copies."""
        self.install_candidate()
        before = self.active_snapshot()
        expected_env = self.fixture.state["fwenv"].copy()
        stop = self.fixture.run_dir / "stop"
        stop.unlink(missing_ok=True)
        result = self.helper.invoke("uninstall", extra=("--dry-run",))
        self.helper.success(result)
        self.assertEqual(before, self.active_snapshot())
        self.assertEqual(expected_env, self.fixture.state["fwenv"])
        self.assertFalse(stop.exists())
        self.assertFalse(list(self.conf.glob("dei-uninstalled.*")))
        for key in MANAGED_SETTINGS:
            self.assertIn(key, result.stdout)

    def test_unknown_or_aliased_script_prevents_partial_cleanup(self) -> None:
        """Preflight all filenames before deleting the hook or touching firmware."""
        self.install_candidate()
        target = self.conf / "status.sh"
        original = target.read_bytes()
        outside = self.fixture.root / "foreign-script"
        outside.write_bytes(original)
        for alias in (False, True):
            with self.subTest(alias=alias):
                target.unlink()
                if alias:
                    target.symlink_to(outside)
                else:
                    target.write_bytes(original + b"# operator change\n")
                before = self.active_snapshot()
                result = self.helper.invoke("uninstall")
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(before, self.active_snapshot())
                self.assertEqual(alias, target.is_symlink())
                self.assertEqual(original, outside.read_bytes())

    def test_original_value_symlink_is_rejected(self) -> None:
        """Ensure the recovery record cannot redirect reads of a saved setting."""
        self.install_candidate()
        before = self.active_snapshot()
        value = self.conf / "setup-state/fwenv.originals/8311_failsafe_delay.value"
        value.symlink_to(self.unrelated)
        result = self.helper.invoke("uninstall")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self.active_snapshot())
        self.assertTrue(value.is_symlink())
        self.assertEqual("keep my local helper\n", self.unrelated.read_text())

    def test_later_operator_edit_preserves_all_settings_and_scripts(self) -> None:
        """Detect drift before the first firmware write or runtime removal."""
        self.install_candidate()
        before = self.active_snapshot()
        self.fixture.state["fwenv"]["mib_file"] = "/etc/mibs/operator-choice.ini"
        expected_env = self.fixture.state["fwenv"].copy()
        result = self.helper.invoke("uninstall")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(expected_env, self.fixture.state["fwenv"])
        self.assertEqual(before, self.active_snapshot())

    def test_failed_firmware_read_is_never_treated_as_absence(self) -> None:
        """Refuse uncertain firmware status, even when failure prints a plausible value."""
        self.install_candidate()
        self.helper.proxies += (
            '\nfw_printenv() { if [ "$2" = 8311_failsafe_delay ]; then '
            'echo 15; echo "read failed" >&2; return 1; fi; "$DEI835_MOCK_BIN/fw_printenv" "$@"; }\n'
        )
        before = self.active_snapshot()
        result = self.helper.invoke("uninstall")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self.active_snapshot())

    def test_retry_completes_a_partial_firmware_restore(self) -> None:
        """Accept an already restored key after a later firmware write fails."""
        self.install_candidate()
        initial_proxies = self.helper.proxies
        self.helper.proxies += (
            "\nfw_setenv() {\n"
            '  if [ -f "$DEI835_MOCK_STATE.fail-next" ]; then return 1; fi\n'
            '  "$DEI835_MOCK_BIN/fw_setenv" "$@" || return 1\n'
            '  : >"$DEI835_MOCK_STATE.fail-next"\n'
            "}\n"
        )
        result = self.helper.invoke("uninstall", allow_fw=True)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, len(self.fixture.state["mutations"]))
        self.helper.proxies = initial_proxies
        self.fixture.state["mutations"] = []
        self.helper.success(self.helper.invoke("uninstall", allow_fw=True))
        self.assertEqual(self.original_env, self.fixture.state["fwenv"])
        self.assert_clean_active_paths()
        self.assertEqual(1, len(list(self.conf.glob("dei-uninstalled.*"))))

    def test_uninstall_respects_firmware_and_worker_locks(self) -> None:
        """Preserve active code when either a firmware hook or runtime worker owns its lock."""
        self.install_candidate()
        before = self.active_snapshot()
        for file in (self.fixture.root / "firmware.lock", self.fixture.run_dir / "worker.lock"):
            with self.subTest(lock=file.name), file.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertNotEqual(0, self.helper.invoke("uninstall").returncode)
                self.assertEqual(before, self.active_snapshot())

    def test_retry_finishes_a_partial_archive(self) -> None:
        """Complete interrupted recovery moves into a single retained private archive."""
        self.install_candidate()
        initial_proxies = self.helper.proxies
        self.helper.proxies += (
            '\nmv() { case "$1" in "$DEI835_CONF_DIR/dei-pon.toml") return 1;; esac; "$DEI_TEST_BUSYBOX" mv "$@"; }\n'
        )
        result = self.helper.invoke("uninstall", allow_fw=True)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(self.original_env, self.fixture.state["fwenv"])
        self.assert_clean_active_paths()
        archives = sorted(self.conf.glob("dei-uninstalled.*"))
        self.assertEqual(1, len(archives))
        archive = archives[0]
        self.assertTrue((archive / "setup-state/fwenv.originals/owner").is_file())
        self.assertTrue((self.conf / "dei-pon.toml").is_file())
        self.helper.proxies = initial_proxies
        self.fixture.state["mutations"] = []
        self.helper.success(self.helper.invoke("uninstall"))
        self.assert_clean_active_paths()
        self.assertEqual(archives, sorted(self.conf.glob("dei-uninstalled.*")))
        self.assertFalse((self.conf / ".dei-uninstall-archive").exists())
        self.assertEqual(CANDIDATE, (archive / "dei-pon.toml").read_text())
        self.assertFalse((archive / "dei835-originals").exists())
