#!/usr/bin/env python3
"""End-to-end installer branches for the optional binary patch, offline only."""

import unittest

import test_installer as installer_support
import test_libponnet as binary_support
import test_persistence as support
from paths import BUSYBOX


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class InstallerBinaryTests(unittest.TestCase):
    """Check guarded library operations through the complete installer."""

    def setUp(self) -> None:
        """Prepare a synthetic library and an offline maintenance state."""
        self.helper = installer_support.InstallerTests("test_check_does_not_install_or_modify_configuration")
        self.helper.setUp()
        self.helper.config.write_text("schema_version=1\n[library_patch]\nenabled=true\n")
        self.fixture = self.helper.fixture
        self.module = self.fixture.root / "module"
        self.library = self.module / "usr/lib/libponnet.so.0.0.0"
        self.library.parent.mkdir(parents=True, exist_ok=True)
        self.library.write_bytes(binary_support.STOCK_BYTES)
        proc = self.module / "proc"
        proc.mkdir()
        (proc / "mounts").write_text(
            "/dev/ubi0_2 /overlay ubifs rw,relatime 0 0\noverlay / overlay rw,relatime,upperdir=/overlay/upper 0 0\n"
        )
        (proc / "sys/kernel/random").mkdir(parents=True)
        (proc / "sys/kernel/random/boot_id").write_text("boot-two\n")
        (self.module / "etc").mkdir(exist_ok=True)
        (self.module / "etc/dei-setup-persist").write_text("boot-one\n")
        (self.helper.conf / "setup-state").mkdir()
        (self.helper.conf / "setup-state/persistence.pending").write_text("boot-one\n")
        # The virtual module MIB is independent of the installer-script directory.
        self.mib = self.module / "ptconf/8311/dei.ini"
        self.mib.parent.mkdir(parents=True, exist_ok=True)
        self.mib.write_text("11 0x0205 0 0\n")
        self.failsafe = self.module / "ptconf/.failsafe"
        self.failsafe.touch()
        self.fixture.state["fwenv"] = {
            "mib_file": "/ptconf/8311/dei.ini",
            "8311_persist_root": "1",
            "bootcmd": "run ubi_init; run flash_flash",
            "8311_gpon_sn": "ZTEG1234ABCD",
            "8311_omcc_version": "0xA0",
            "8311_failsafe_delay": "30",
        }
        stock = self.fixture.root / "synthetic-stock.bin"
        stock.write_bytes(binary_support.STOCK_BYTES)
        self.helper.env["DEI835_MODULE_ROOT"] = str(self.module)
        self.helper.env["LIBPATCH_STOCK_FIXTURE"] = str(stock)

    def tearDown(self) -> None:
        """Remove the temporary files created for this test."""
        self.helper.tearDown()

    def test_check_reports_offline_applicability_without_patching(self) -> None:
        """Assess library applicability offline without changing the library."""
        self.fixture.state["pon_state"] = 11
        result = self.helper.invoke("check")
        self.helper.success(result)
        self.assertIn("binary patch needed=1", result.stdout)
        self.assertEqual(binary_support.STOCK_BYTES, self.library.read_bytes())
        self.helper.assert_runtime_absent()
        self.assertFalse((self.helper.conf / "dei835-libponnet.stock").exists())

    def test_offline_install_patches_and_installs_helpers_without_live_o5(self) -> None:
        """Install verified maintenance changes without requiring PON activation."""
        self.fixture.state["pon_state"] = 11
        result = self.helper.invoke("install")
        self.helper.success(result)
        self.assertIn("planned reboot is required", result.stdout)
        self.assertEqual(binary_support.PATCHED_BYTES, self.library.read_bytes())
        self.assertEqual(binary_support.STOCK_BYTES, (self.helper.conf / "dei835-libponnet.stock").read_bytes())
        self.assertEqual((support.SOURCE / "keyring.sh").read_bytes(), (self.helper.conf / "keyring.sh").read_bytes())
        self.assertTrue(self.failsafe.exists())
        self.assertFalse(any(call[0] == "omci_pipe.sh" for call in self.fixture.state["calls"]))
        inode = self.library.stat().st_ino
        self.helper.success(self.helper.invoke("install"))
        self.assertEqual(inode, self.library.stat().st_ino)

    def test_working_live_session_installs_helpers_but_defers_binary(self) -> None:
        """Defer library replacement while the OMCI daemon is running."""
        self.failsafe.unlink()
        comm = self.module / "proc/123/comm"
        comm.parent.mkdir()
        comm.write_text("omcid\n")
        result = self.helper.invoke("install")
        self.helper.success(result)
        self.assertIn("deferred: omcid", result.stdout)
        self.assertEqual(binary_support.STOCK_BYTES, self.library.read_bytes())
        self.assertFalse((self.helper.conf / "dei835-libponnet.stock").exists())

    def test_native_uni_skips_binary_and_uses_live_preflight(self) -> None:
        """Skip the incompatible binary patch for a native UNI layout."""
        self.mib.write_text("11 0x0101 0 0\n")
        self.failsafe.unlink()
        result = self.helper.invoke("install")
        self.helper.success(result)
        self.assertIn("not-applicable", result.stdout)
        self.assertEqual(binary_support.STOCK_BYTES, self.library.read_bytes())
        self.assertTrue(any(call[0] == "pon" for call in self.fixture.state["calls"]))

    def test_unknown_binary_is_not_modified_even_on_healthy_install(self) -> None:
        """Reject unsupported library contents without modifying their bytes."""
        unknown = b"UNSUPPORTED LIBRARY"
        self.library.write_bytes(unknown)
        self.assertNotEqual(0, self.helper.invoke("install").returncode)
        self.assertEqual(unknown, self.library.read_bytes())
        self.assertNotEqual(0, self.helper.invoke("patch-library").returncode)

    def test_script_rollback_preserves_installer_owned_binary(self) -> None:
        """Keep binary rollback separate from runtime-script rollback."""
        self.helper.success(self.helper.invoke("install"))
        self.helper.success(self.helper.invoke("rollback"))
        self.helper.assert_runtime_absent()
        self.assertEqual(binary_support.PATCHED_BYTES, self.library.read_bytes())
        self.assertTrue((self.helper.conf / "dei835-libponnet.owner").exists())
        self.helper.success(self.helper.invoke("rollback-library"))
        self.assertEqual(binary_support.STOCK_BYTES, self.library.read_bytes())

    def test_preexisting_patch_is_preserved_without_claiming_ownership(self) -> None:
        """Preserve an existing patch and refuse to restore an unowned library."""
        self.library.write_bytes(binary_support.PATCHED_BYTES)
        self.helper.success(self.helper.invoke("install"))
        self.helper.success(self.helper.invoke("rollback"))
        self.assertEqual(binary_support.PATCHED_BYTES, self.library.read_bytes())
        self.assertNotEqual(0, self.helper.invoke("rollback-library").returncode)

    def test_patch_library_action_is_reachable_without_running_pon(self) -> None:
        """Allow guarded library maintenance without requiring an active PON link."""
        self.fixture.state["pon_state"] = 11
        result = self.helper.invoke("patch-library")
        self.helper.success(result)
        self.assertEqual(binary_support.PATCHED_BYTES, self.library.read_bytes())
        self.helper.assert_runtime_absent()
        self.assertFalse(any(call[0] == "omci_pipe.sh" for call in self.fixture.state["calls"]))


if __name__ == "__main__":
    unittest.main()
