"""Installer/rollback tests using the same offline fake module as the worker."""

import fcntl
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import cast

import test_persistence as support
from paths import BUSYBOX, DIST, FIXTURES

RUNTIME = ("dei-config.sh", "dei-835-lib.sh", "keyring.sh", "status.sh", "vlan_fixes_hook.sh")


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class InstallerTests(unittest.TestCase):
    """Exercise installation and rollback against an isolated fake module."""

    def setUp(self) -> None:
        """Create isolated filesystem state and mock every device-facing command."""
        assert BUSYBOX is not None
        self.busybox = BUSYBOX
        self.fixture = support.PersistenceTests("test_check_reports_actual_gems_and_makes_no_changes")
        self.fixture.setUp()
        self.conf = self.fixture.conf
        for name in RUNTIME:
            (self.conf / name).unlink(missing_ok=True)
        self.module = self.fixture.root / "module"
        (self.module / "ptconf/8311").mkdir(parents=True)
        module_bin = self.module / "usr/bin"
        module_bin.mkdir(parents=True)
        shutil.copyfile(self.fixture.bin / "mock_device.py", module_bin / "pon")
        (module_bin / "pon").chmod(0o755)
        (self.module / "usr/lib").mkdir(parents=True)
        (self.module / "usr/lib/libponnet.so.0.0.0").write_bytes(b"Runtime fixture library; binary patch unsupported")
        native = self.module / "etc/mibs/prx300_1U.ini"
        native.parent.mkdir(parents=True)
        native.write_text("11 0x0101 0 0\n")
        self.config = self.fixture.root / "input.toml"
        self.config.write_text("schema_version = 1\n")
        (self.fixture.bin / "fw_setenv").symlink_to(self.fixture.bin / "mock_device.py")
        self.fixture.state = support.initial_state(support.FIXED_RULE)
        self.fixture.state["fwenv"] = {
            "8311_gpon_sn": "ZTEG1234ABCD",
            "8311_omcc_version": "0xA0",
            "8311_failsafe_delay": "30",
            "mib_file": "/etc/mibs/prx300_1U.ini",
        }
        for gem in ("1084", "1085", "1086", "1087"):
            self.fixture.state["gems"][gem]["enc"] = 3
        for dev in self.fixture.state["mappers"]:
            (self.fixture.net / dev).touch()
        hash_mock = self.fixture.bin / "md5sum"
        hash_mock.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            'sys.path.insert(0, os.environ["DEI_TEST_HELPERS"])\n'
            "from fixture_hash import main\n"
            "raise SystemExit(main())\n"
        )
        hash_mock.chmod(0o755)
        self.proxies = "\n".join(
            f'{name}() {{ "$DEI835_MOCK_BIN/{name}" "$@"; }}'
            for name in ("ip", "tc", "pon", "sleep", "fw_printenv", "md5sum", "sh")
        )
        launcher = self.fixture.bin / "launch-ash"
        launcher.write_text(self.proxies + '\nscript=$1; shift; . "$script"\n')
        wrapper = self.fixture.bin / "sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = -n ]; then exec "$DEI_TEST_BUSYBOX" ash "$@"; fi\n'
            'exec "$DEI_TEST_BUSYBOX" ash "$DEI835_MOCK_BIN/launch-ash" "$@"\n'
        )
        wrapper.chmod(0o755)
        self.env = {
            **self.fixture.env,
            "PATH": f"{module_bin}:{self.fixture.env['PATH']}",
            "DEI835_FIRMWARE_LOCK": str(self.fixture.root / "firmware.lock"),
            "DEI835_SKIP_START": "1",
            "DEI835_MODULE_ROOT": str(self.module),
            "DEI_PON_CONFIG_PATH": str(self.config),
            "DEI_TEST_BUSYBOX": BUSYBOX,
            "DEI_TEST_HELPERS": str(Path(__file__).resolve().parent),
        }

    def tearDown(self) -> None:
        """Remove the temporary files created for this test."""
        self.fixture.tearDown()

    def invoke(
        self,
        action: str,
        installer: Path | None = None,
        *,
        extra: tuple[str, ...] = (),
        allow_fw: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run an installer action and verify its permitted mock mutations.

        Returns:
            The captured installer process result.

        """
        self.fixture.state_path.write_text(json.dumps(self.fixture.state))
        result = subprocess.run(
            [
                self.busybox,
                "ash",
                "-c",
                self.proxies + '\nscript=$1; shift; . "$script"\n',
                "offline-installer",
                str(installer or DIST / "dei-pon-install.sh"),
                action,
                *extra,
            ],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.fixture.state = cast("support.ModuleMockState", json.loads(self.fixture.state_path.read_text()))
        self.assertFalse(self.fixture.state.get("mock_errors"), result.stdout + result.stderr)
        if allow_fw:
            self.assertTrue(all(m["kind"] == "fwenv_set" for m in self.fixture.state["mutations"]))
        else:
            self.assertEqual([], self.fixture.state["mutations"])
        self.assertNotIn("ZTEG1234ABCD", result.stdout + result.stderr)
        return result

    def success(self, result: subprocess.CompletedProcess[str]) -> None:
        """Require a successful command and include captured output on failure."""
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def assert_runtime_absent(self) -> None:
        """Verify all five managed runtime paths are absent."""
        for name in RUNTIME:
            self.assertFalse(os.path.lexists(self.conf / name), name)

    def test_check_does_not_install_or_modify_configuration(self) -> None:
        """Verify check leaves installed scripts and backup state untouched."""
        self.success(self.invoke("check"))
        self.assert_runtime_absent()
        self.assertFalse((self.conf / "dei835-originals").exists())
        self.assertFalse((self.conf / "dei-835-lib.sh").exists())
        self.assertFalse((self.conf / "install.log").exists())

    def test_clean_install_is_idempotent_and_rollback_removes_runtime(self) -> None:
        """Install only bundled files, then roll back to empty runtime paths."""
        self.success(self.invoke("install"))
        for name in RUNTIME:
            self.assertEqual((support.SOURCE / name).read_bytes(), (self.conf / name).read_bytes())
            self.assertTrue(os.access(self.conf / name, os.X_OK))
        self.success(self.invoke("install"))
        self.assertFalse((self.conf / "dei835-originals").exists())
        self.success(self.invoke("rollback"))
        self.assert_runtime_absent()
        self.success(self.invoke("rollback"))
        self.assert_runtime_absent()

    def test_foreign_hook_blocks_managed_operations_before_mutations(self) -> None:
        """Refuse foreign contents at a managed path, including guide-era hooks."""
        hook = self.conf / "vlan_fixes_hook.sh"
        content = (FIXTURES / "guide/vlan_fixes_hook.sh").read_bytes()
        hook.write_bytes(content)
        for action in ("check", "install", "patch-library", "rollback", "uninstall"):
            with self.subTest(action=action):
                result = self.invoke(action)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(hook.name, result.stderr)
                self.assertEqual(content, hook.read_bytes())
                self.assertEqual({hook.name}, {p.name for p in self.conf.iterdir()})
                self.assertFalse(self.fixture.run_dir.exists())

    def test_wrong_mode_is_planned_without_changes_during_check(self) -> None:
        """Report required mode changes without applying them during check."""
        self.fixture.state["fix_mode"] = 1
        self.success(self.invoke("check"))
        self.assert_runtime_absent()

    def test_ambiguous_discovery_is_reported_without_guessing(self) -> None:
        """Report multiple matching service mappers without choosing one."""
        self.fixture.state["mappers"]["pmapper2"]["rules"] = support.FIXED_RULE
        result = self.invoke("check")
        self.success(result)
        self.assertIn("multiple VLAN-835 mappers", result.stdout)
        self.assert_runtime_absent()

    def test_failed_checksum_command_cannot_validate_payload(self) -> None:
        """Refuse installation even if a failing checksum tool prints a matching digest."""
        self.proxies += '\nsha256sum() { "$DEI_TEST_BUSYBOX" sha256sum "$@"; return 1; }\n'
        result = self.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Payload checksum failed", result.stderr)
        self.assert_runtime_absent()
        self.assertFalse((self.conf / "dei835-originals").exists())

    def test_modified_payload_fails_checksum_before_install(self) -> None:
        """Reject a corrupted embedded payload before changing installed files."""
        corrupted = self.fixture.root / "corrupted.sh"
        text = (DIST / "dei-pon-install.sh").read_text()
        corrupted.write_text(text.replace("No device identity, MIB or bridge-port changes.", "CORRUPTED PAYLOAD", 1))
        result = self.invoke("install", corrupted)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Payload checksum failed", result.stderr)
        self.assert_runtime_absent()

    def test_partial_project_install_finishes_from_known_payloads(self) -> None:
        """Complete a partial installation with recognized project files."""
        for name in ("dei-835-lib.sh", "keyring.sh"):
            shutil.copyfile(support.SOURCE / name, self.conf / name)
        self.success(self.invoke("install"))
        self.assertEqual(
            (support.SOURCE / "vlan_fixes_hook.sh").read_bytes(), (self.conf / "vlan_fixes_hook.sh").read_bytes()
        )
        self.success(self.invoke("rollback"))
        self.assert_runtime_absent()

    def test_rollback_refuses_to_overwrite_later_user_edits(self) -> None:
        """Preserve installed scripts that the user edited after installation."""
        self.success(self.invoke("install"))
        changed = (self.conf / "status.sh").read_text() + "\n# edited\n"
        (self.conf / "status.sh").write_text(changed)
        result = self.invoke("rollback")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(changed, (self.conf / "status.sh").read_text())

    def test_rollback_waits_for_once_worker_lock_without_pid(self) -> None:
        """Respect a live worker lock even when no watcher PID file exists."""
        self.success(self.invoke("install"))
        with (self.fixture.run_dir / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.invoke("rollback")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("still finishing", result.stderr)
            self.assertEqual((support.SOURCE / "keyring.sh").read_bytes(), (self.conf / "keyring.sh").read_bytes())
        self.success(self.invoke("rollback"))
        self.assert_runtime_absent()

    def test_install_ignores_preexisting_predictable_staging_symlinks(self) -> None:
        """Keep unrelated files intact when legacy staging names are symlinks."""
        for name in ("keyring.sh.dei-new", "dei-pon.toml.new"):
            outside = self.fixture.root / (name + ".unrelated")
            outside.write_text("preserve unrelated data\n")
            (self.conf / name).symlink_to(outside)
        self.success(self.invoke("install"))
        for name in ("keyring.sh.dei-new", "dei-pon.toml.new"):
            outside = self.fixture.root / (name + ".unrelated")
            self.assertEqual("preserve unrelated data\n", outside.read_text())
            self.assertTrue((self.conf / name).is_symlink())
        self.assertFalse((self.conf / "keyring.sh").is_symlink())
        self.assertFalse((self.conf / "dei-pon.toml").is_symlink())
        self.assertEqual([], list(self.conf.glob(".dei-pon-stage.*")))

    def test_rollback_ignores_preexisting_predictable_restore_symlink(self) -> None:
        """Restore scripts without following a legacy staging alias."""
        self.success(self.invoke("install"))
        outside = self.fixture.root / "unrelated-restore.txt"
        outside.write_text("preserve rollback sentinel\n")
        (self.conf / "keyring.sh.dei-restore").symlink_to(outside)
        self.success(self.invoke("rollback"))
        self.assertEqual("preserve rollback sentinel\n", outside.read_text())
        self.assertTrue((self.conf / "keyring.sh.dei-restore").is_symlink())
        self.assertFalse((self.conf / "keyring.sh").is_symlink())
        self.assert_runtime_absent()
        self.assertEqual([], list(self.conf.glob(".dei-pon-stage.*")))

    def test_dangling_configuration_backup_link_is_preserved(self) -> None:
        """Refuse a dangling backup alias without creating its target."""
        backup = self.conf / "setup-state"
        backup.mkdir()
        (self.conf / "dei-pon.toml").write_text("schema_version = 1\n")
        outside = self.fixture.root / "missing-private-file"
        (backup / "dei-pon.toml.before").symlink_to(outside)
        result = self.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(outside.exists())
        self.assertTrue((backup / "dei-pon.toml.before").is_symlink())
        self.assert_runtime_absent()

    def test_existing_configuration_backup_is_not_overwritten(self) -> None:
        """Preserve a prior regular configuration backup during installation."""
        backup = self.conf / "setup-state"
        backup.mkdir()
        (backup / "dei-pon.toml.before").write_text("earlier retained configuration\n")
        (self.conf / "dei-pon.toml").write_text("schema_version = 1\n")
        self.success(self.invoke("install"))
        self.assertEqual("earlier retained configuration\n", (backup / "dei-pon.toml.before").read_text())

    def test_saved_configuration_alias_is_rejected(self) -> None:
        """Preserve an aliased canonical configuration instead of replacing it."""
        outside = self.fixture.root / "external-saved.toml"
        outside.write_text("schema_version = 1\n")
        (self.conf / "dei-pon.toml").symlink_to(outside)
        result = self.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Saved configuration is a symlink", result.stderr)
        self.assertEqual("schema_version = 1\n", outside.read_text())
        self.assert_runtime_absent()

    def test_installed_script_alias_is_rejected_before_hashing(self) -> None:
        """Preserve an installed-script alias even when its contents are recognized."""
        outside = self.fixture.root / "external-keyring.sh"
        shutil.copyfile(support.SOURCE / "keyring.sh", outside)
        (self.conf / "keyring.sh").symlink_to(outside)
        original = outside.read_bytes()
        result = self.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("keyring.sh", result.stderr)
        self.assertEqual(original, outside.read_bytes())
        self.assertTrue((self.conf / "keyring.sh").is_symlink())

    def test_configuration_fifo_is_rejected_before_copying(self) -> None:
        """Reject a FIFO immediately instead of blocking while staging input."""
        fifo = self.fixture.root / "input.fifo"
        os.mkfifo(fifo)
        result = self.invoke("install", extra=("--config", str(fifo)))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("readable regular file", result.stderr)
        self.assert_runtime_absent()
        self.assertFalse((self.conf / "install.log").exists())

    def test_oversized_configuration_is_rejected_before_copying(self) -> None:
        """Enforce the input-size limit before creating a staged config copy."""
        self.config.write_text("#" * 65537)
        result = self.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("exceeds 64 KiB", result.stderr)
        self.assert_runtime_absent()
        self.assertFalse((self.conf / "install.log").exists())

    def test_explicit_configuration_symlink_to_regular_file_is_readable(self) -> None:
        """Allow a user-selected input alias while saving a real private config file."""
        alias = self.fixture.root / "input-alias.toml"
        alias.symlink_to(self.config)
        self.success(self.invoke("install", extra=("--config", str(alias))))
        self.assertFalse((self.conf / "dei-pon.toml").is_symlink())
        self.assertEqual(self.config.read_bytes(), (self.conf / "dei-pon.toml").read_bytes())

    def test_failed_unique_staging_copy_preserves_installed_scripts(self) -> None:
        """Clean a failed private staging file without modifying its destination."""
        self.proxies += (
            "\ncp() {\n"
            "    for copy_arg; do copy_last=$copy_arg; done\n"
            '    case "$copy_last" in "$DEI835_CONF_DIR"/.dei-pon-stage.*) return 1;; esac\n'
            '    "$DEI_TEST_BUSYBOX" cp "$@"\n'
            "}\n"
        )
        result = self.invoke("install")
        self.assertNotEqual(0, result.returncode)
        self.assert_runtime_absent()
        self.assertEqual([], list(self.conf.glob(".dei-pon-stage.*")))


if __name__ == "__main__":
    unittest.main()
