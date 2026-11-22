"""Offline setup-phase tests. All module state and commands are synthetic."""

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from paths import BUSYBOX, SOURCE

ROM = (
    "# synthetic ROM fixture\n11 0x0101 0 0\n5 0x0101 0\n"
    '6 0x0101 0 0 0 "OLDHW" "OLDD"\n264 0x0101 0\n'
    '256 0x0000 "OLDD" "OLDHW"\n257 0x0000 "OLDEQ"\n'
    + "".join(f"277 0x00{128 + i:02x} 0x0101000{i} end\n" for i in range(8))
)
DEI = ROM
for _class, _target in ((11, "0x0205"), (5, "0x0201"), (6, "0x0201"), (264, "0x0205")):
    DEI = re.sub(rf"^{_class} 0x0101 ", f"{_class} {_target} ", DEI, flags=re.MULTILINE)
for _i in range(8):
    DEI = DEI.replace(f" 0x0101000{_i} ", f" 0x0205000{_i} ")
MOCK = r"""#!/usr/bin/env python3
import hashlib,json,os,pathlib,sys
root=pathlib.Path(os.environ['FAKE_ROOT']); name=pathlib.Path(sys.argv[0]).name
db=root/'env.json'; env=json.loads(db.read_text())
if name=='fw_printenv':
    key=sys.argv[-1]
    if key not in env:
        print(f'## Error: "{key}" not defined', file=sys.stderr)
        sys.exit(1)
    print(env[key]); sys.exit(1 if os.environ.get('FAIL_READ')==key else 0)
if name=='fw_setenv':
    key=sys.argv[1]
    if os.environ.get('FAIL_WRITE')==key: sys.exit(0)
    if len(sys.argv)>2: env[key]=sys.argv[2]
    else: env.pop(key,None)
    db.write_text(json.dumps(env))
    with (root/'writes.jsonl').open('a') as f: f.write(json.dumps(sys.argv[1:])+'\n')
    sys.exit(0)
if name=='md5sum':
    p=pathlib.Path(sys.argv[1])
    try: data=p.read_bytes()
    except OSError: sys.exit(1)
    known={b'stock library':'5326f83e22a18929b830adfb82cfc483',b'patched library':'5ddb892fbf0b648479f605d9fddda50c'}
    known[(root/'rom-fixture').read_bytes()]='924ac47bce92553361928dd3fa61bec7'
    known[(root/'dei-fixture').read_bytes()]='1573d7b59c093751df3531e8b603d3b5'
    print(known.get(data,hashlib.md5(data).hexdigest()),p)
    sys.exit(0)
sys.exit(92)
"""


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class SetupTests(unittest.TestCase):
    """Exercise setup planning, maintenance phases, and ownership safeguards offline."""

    def setUp(self) -> None:
        """Create isolated module files and replace device commands with controlled fakes."""
        self.temp = tempfile.TemporaryDirectory(prefix="dei-setup-test.")
        self.root = Path(self.temp.name)
        for directory in ("bin", "ptconf/8311", "etc/mibs", "rom/etc/mibs", "usr/lib", "proc/sys/kernel/random"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "rom-fixture").write_text(ROM)
        (self.root / "dei-fixture").write_text(DEI)
        self.put("rom/etc/mibs/prx300_1U.ini", ROM)
        self.put("etc/mibs/prx300_1U.ini", ROM)
        self.put("usr/lib/libponnet.so.0.0.0", "patched library")
        self.put("ptconf/8311/dei.ini", DEI)
        self.put("proc/sys/kernel/random/boot_id", "boot-one\n")
        self.put(
            "proc/mounts",
            "ubi0:rootfs_data /overlay ubifs rw 0 0\noverlay / overlay rw,lowerdir=/rom,upperdir=/overlay/upper,workdir=/overlay/work 0 0\n",
        )
        self.env_values = {
            "8311_gpon_sn": "ZTEG1234ABCD",
            "8311_omcc_version": "0xA0",
            "mib_file": "/ptconf/8311/dei.ini",
            "8311_fix_vlans": "2",
            "8311_failsafe_delay": "30",
            "8311_persist_root": "1",
            "bootcmd": "run ubi_init; run flash_flash",
            "8311_ipaddr": "192.0.2.1",
            "8311_netmask": "255.255.255.0",
            "8311_gateway": "",
            "8311_ping_ip": "192.0.2.2",
        }
        self.save_env()
        for name in ("fw_printenv", "fw_setenv", "md5sum"):
            self.put("bin/" + name, MOCK)
            (self.root / "bin" / name).chmod(0o755)
        self.config = {
            "CFG_ONU_SERIAL": "",
            "CFG_ONU_OMCC_VERSION": "0xA0",
            "CFG_ONU_VENDOR_ID": "",
            "CFG_ONU_EQUIPMENT_ID": "",
            "CFG_ONU_HARDWARE_VERSION": "",
            "CFG_ONU_MIB_PROFILE": "preserve",
            "CFG_MANAGEMENT_IP_ADDRESS": "",
            "CFG_MANAGEMENT_NETMASK": "",
            "CFG_MANAGEMENT_GATEWAY": "",
            "CFG_MANAGEMENT_PING_IP": "",
            "CFG_MANAGEMENT_FAILSAFE_DELAY": "30",
            "CFG_STORAGE_PERSISTENT_ROOT": "auto",
            "CFG_LIBRARY_PATCH_ENABLED": "true",
            "CFG_LIBRARY_PATCH_POLICY": "auto",
        }

    def tearDown(self) -> None:
        """Remove the temporary filesystem created for this test."""
        self.temp.cleanup()

    def put(self, path: str | Path, data: str) -> None:
        """Write text into the isolated module tree, creating its parent directories.

        Args:
            path: Module-relative destination path.
            data: Text to write into the fixture file.

        """
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def save_env(self) -> None:
        """Save the fake firmware environment as the mock command state."""
        self.put("env.json", json.dumps(self.env_values))

    def current_env(self) -> dict[str, str]:
        """Read the firmware environment saved by the mock commands.

        Returns:
            The current fake firmware variables and their string values.

        """
        return cast("dict[str, str]", json.loads((self.root / "env.json").read_text()))

    def snapshot(self) -> dict[str, bytes]:
        """Capture the relative path and bytes of every fixture file.

        Returns:
            A file-content snapshot suitable for checking read-only operations.

        """
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def run_engine(
        self, operation: str = "setup_apply", *, extra: str = "", **env: str
    ) -> subprocess.CompletedProcess[str]:
        """Invoke setup functions against the isolated module tree.

        Args:
            operation: Initial setup function or shell expression to execute.
            extra: Additional controlled shell statements for the scenario.
            **env: Environment overrides used by the mock commands.

        Returns:
            Captured shell result, including the final setup flags.

        """
        script = f"""set -u
D="$FAKE_ROOT/ptconf/8311"
R="$FAKE_ROOT/tmp/dei835"
# Standalone BusyBox ash may select its applet before the mock PATH entry.
md5sum() {{ "$FAKE_ROOT/bin/md5sum" "$@"; }}
. "{SOURCE / "checksums.sh"}"
. "{SOURCE / "libponnet-patch.sh"}"
. "{SOURCE / "setup-engine.sh"}"
{operation} || exit $?
{extra}
printf 'FLAGS reboot=%s maintenance=%s config=%s patch=%s\\n' "$SETUP_REBOOT_REQUIRED" "$SETUP_MAINTENANCE_READY" "$SETUP_CONFIG_READY" "$SETUP_PATCH_REQUIRED"
"""
        e = dict(os.environ, **self.config, **env)
        e.update(
            FAKE_ROOT=str(self.root),
            DEI835_MODULE_ROOT=str(self.root),
            PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
        )
        shell_override = os.environ.get("SETUP_TEST_SHELL")
        shell = shlex.split(shell_override) if shell_override else [BUSYBOX, "ash"]
        return subprocess.run([*shell, "-c", script], text=True, capture_output=True, env=e, check=False)

    def fresh(self, *, serial: bool = True) -> None:
        """Reset the fixture to a stock library and a fresh DEI setup request.

        Args:
            serial: Whether the request supplies a synthetic provisioned ONU serial.

        """
        self.env_values = {"bootcmd": "run ubi_init;ubi remove rootfs_data;run flash_flash"}
        self.save_env()
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        (self.root / "ptconf/8311/dei.ini").unlink()
        self.config["CFG_ONU_MIB_PROFILE"] = "dei_0205"
        if serial:
            self.config["CFG_ONU_SERIAL"] = "ZTEGABCD1234"

    def second_boot(self, *, lose_marker: bool = False) -> None:
        """Simulate another boot without restarting any host process.

        Args:
            lose_marker: Whether to simulate an overlay that discarded its marker.

        """
        self.put("proc/sys/kernel/random/boot_id", "boot-two\n")
        if lose_marker:
            (self.root / "etc/dei-setup-persist").unlink()
        p = self.root / "proc/42/comm"
        if p.exists():
            p.unlink()

    def test_check_is_read_only_and_hides_serial(self) -> None:
        """Verify that check is read-only and hides serial."""
        before = self.snapshot()
        r = self.run_engine("setup_check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn("ZTEG1234ABCD", r.stdout + r.stderr)

    def test_working_preserve_no_reboot_or_fw_writes(self) -> None:
        """Verify that working preserve no reboot or firmware writes."""
        r = self.run_engine(extra="setup_finish || exit $?")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reboot=0 maintenance=0 config=1 patch=0", r.stdout)
        self.assertFalse((self.root / "writes.jsonl").exists())
        self.assertEqual((self.root / "ptconf/8311/dei.ini").read_text(), DEI)

    def test_blank_management_preserves_current_values(self) -> None:
        """Verify that blank management preserves current values."""
        before = self.current_env()
        self.assertEqual(self.run_engine().returncode, 0)
        self.assertEqual(before, self.current_env())

    def test_backup_preserves_unset_and_empty_without_printing(self) -> None:
        """Verify that backup preserves unset and empty without printing."""
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        b = self.root / "ptconf/8311/setup-state/fwenv.originals"
        self.assertEqual((b / "8311_vendor_id.state").read_text(), "absent\n")
        self.assertEqual((b / "8311_gateway.state").read_text(), "present\n")
        self.assertEqual((b / "8311_gateway.value").read_text(), "")
        self.assertEqual((b / "8311_gpon_sn.value").stat().st_mode & 0o777, 0o600)

    def test_native_identity_management_updates_need_reboot(self) -> None:
        """Verify that native identity management updates need reboot."""
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        self.env_values["mib_file"] = "/etc/mibs/prx300_1U.ini"
        self.save_env()
        self.config.update(
            CFG_ONU_MIB_PROFILE="native_1u", CFG_ONU_SERIAL="ZTEGAAAABBBB", CFG_MANAGEMENT_IP_ADDRESS="198.51.100.1"
        )
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reboot=1 maintenance=0 config=1 patch=0", r.stdout)
        self.assertEqual(self.current_env()["8311_ipaddr"], "198.51.100.1")
        self.assertNotIn("ZTEGAAAABBBB", r.stdout + r.stderr)

    def test_fresh_missing_serial_fails_before_writes(self) -> None:
        """Verify that fresh missing serial fails before writes."""
        self.fresh(serial=False)
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_preserve_needs_explicit_mib_profile(self) -> None:
        """Verify that fresh preserve needs explicit MIB profile."""
        self.fresh()
        self.config["CFG_ONU_MIB_PROFILE"] = "preserve"
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("choose native_1u or dei_0205", r.stderr)

    def test_unknown_rom_fails_before_identity_changes(self) -> None:
        """Verify that unknown ROM fails before identity changes."""
        self.fresh()
        self.put("rom/etc/mibs/prx300_1U.ini", "different firmware")
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_first_phase_only_prepares_persistence_and_failsafe(self) -> None:
        """Verify that first phase only prepares persistence and failsafe."""
        self.fresh()
        self.put("proc/42/comm", "omcid\n")
        r = self.run_engine(extra="setup_finish || exit $?")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reboot=1 maintenance=0 config=0 patch=1", r.stdout)
        self.assertTrue((self.root / "ptconf/.failsafe").exists())
        self.assertFalse((self.root / "ptconf/8311/dei.ini").exists())
        self.assertEqual((self.root / "usr/lib/libponnet.so.0.0.0").read_text(), "stock library")
        self.assertEqual(self.current_env()["8311_persist_root"], "1")
        self.assertEqual(self.current_env()["bootcmd"], "run ubi_init; run flash_flash")
        self.assertEqual(
            (self.root / "ptconf/8311/setup-state/fwenv.originals/8311_gpon_sn.state").read_text(), "absent\n"
        )

    def test_same_boot_does_not_claim_maintenance_ready(self) -> None:
        """Verify that same boot does not claim maintenance ready."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        before = (self.root / "etc/dei-setup-persist").read_text()
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("maintenance=0 config=0 patch=1", r.stdout)
        self.assertEqual((self.root / "etc/dei-setup-persist").read_text(), before)

    def test_second_phase_builds_mib_but_does_not_patch_library(self) -> None:
        """Verify that second phase builds MIB but does not patch library."""
        self.fresh()
        self.config.update(CFG_ONU_EQUIPMENT_ID="New Eq & model", CFG_ONU_HARDWARE_VERSION="HW-2")
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("maintenance=1 config=1 patch=1", r.stdout)
        mib = (self.root / "ptconf/8311/dei.ini").read_text()
        self.assertIn("11 0x0205", mib)
        self.assertIn('256 0x0000 "ZTEG" "HW-2"', mib)
        self.assertIn('257 0x0000 "New Eq & model"', mib)
        self.assertEqual(self.current_env()["mib_file"], "/ptconf/8311/dei.ini")
        self.assertEqual((self.root / "usr/lib/libponnet.so.0.0.0").read_text(), "stock library")

    def test_finish_requires_patch_before_removing_failsafe(self) -> None:
        """Verify that finish requires patch before removing failsafe."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        r = self.run_engine(extra="setup_finish || exit $?")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.root / "ptconf/.failsafe").exists())

    def test_finish_after_mock_patch_removes_only_owned_failsafe(self) -> None:
        """Verify that finish after mock patch removes only owned failsafe."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        r = self.run_engine(
            extra='printf "patched library" > "$FAKE_ROOT/usr/lib/libponnet.so.0.0.0"\nsetup_finish || exit $?'
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.root / "ptconf/.failsafe").exists())
        self.assertIn("reboot=1 maintenance=1 config=1 patch=1", r.stdout)

    def test_user_failsafe_is_retained(self) -> None:
        """Verify that user failsafe is retained."""
        self.fresh()
        self.put("ptconf/.failsafe", "")
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        r = self.run_engine(
            extra='printf "patched library" > "$FAKE_ROOT/usr/lib/libponnet.so.0.0.0"\nsetup_finish || exit $?'
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.root / "ptconf/.failsafe").exists())
        self.assertIn("Preserved your existing failsafe", r.stdout)

    def test_lost_overlay_marker_is_a_hard_failure(self) -> None:
        """Verify that lost overlay marker is a hard failure."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot(lose_marker=True)
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("did not survive", r.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_bootcmd_is_not_overwritten(self) -> None:
        """Verify that unknown bootcmd is not overwritten."""
        self.fresh()
        self.env_values["bootcmd"] = "run my_custom_boot"
        self.save_env()
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_never_policy_blocks_required_patch(self) -> None:
        """Report a forbidden patch during check and reject apply before writes."""
        self.fresh()
        self.config["CFG_LIBRARY_PATCH_POLICY"] = "never"
        before = self.snapshot()
        check = self.run_engine("setup_check")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("binary patch needed=1", check.stdout)
        self.assertIn('policy = "auto"', check.stdout)
        self.assertEqual(self.snapshot(), before)
        apply = self.run_engine()
        self.assertNotEqual(apply.returncode, 0)
        self.assertIn("enabled = true", apply.stderr)
        self.assertIn('policy = "auto"', apply.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_missing_or_false_patch_opt_in_blocks_all_setup_writes(self) -> None:
        """Require opt-in before backups, firmware variables, MIBs, or failsafe writes."""
        self.fresh()
        for enabled in (None, "false"):
            with self.subTest(enabled=enabled):
                if enabled is None:
                    self.config.pop("CFG_LIBRARY_PATCH_ENABLED", None)
                else:
                    self.config["CFG_LIBRARY_PATCH_ENABLED"] = enabled
                before = self.snapshot()
                check = self.run_engine("setup_check")
                self.assertEqual(check.returncode, 0, check.stderr)
                self.assertIn("binary patch needed=1", check.stdout)
                self.assertIn("enabled = true", check.stdout)
                self.assertEqual(self.snapshot(), before)
                apply = self.run_engine()
                self.assertNotEqual(apply.returncode, 0)
                self.assertIn("enabled = true", apply.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.root / "ptconf/8311/setup-state").exists())
                self.assertFalse((self.root / "writes.jsonl").exists())
                self.assertFalse((self.root / "ptconf/.failsafe").exists())

    def test_preserved_dei_profile_requires_explicit_patch_opt_in(self) -> None:
        """Reject writes for a stock library under a preserved DEI MIB without opt-in."""
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        self.config["CFG_LIBRARY_PATCH_ENABLED"] = "false"
        before = self.snapshot()
        check = self.run_engine("setup_check")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("binary patch needed=1", check.stdout)
        self.assertEqual(self.snapshot(), before)
        apply = self.run_engine()
        self.assertNotEqual(apply.returncode, 0)
        self.assertIn("enabled = true", apply.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_existing_known_patch_does_not_require_opt_in_again(self) -> None:
        """Preserve an already patched installation when new patching is disabled."""
        self.config["CFG_LIBRARY_PATCH_ENABLED"] = "false"
        self.config["CFG_LIBRARY_PATCH_POLICY"] = "never"
        before = self.snapshot()
        check = self.run_engine("setup_check")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("binary patch needed=0", check.stdout)
        self.assertNotIn("enabled = true", check.stdout)
        self.assertEqual(self.snapshot(), before)
        apply = self.run_engine()
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.assertIn("reboot=0 maintenance=0 config=1 patch=0", apply.stdout)
        self.assertFalse((self.root / "writes.jsonl").exists())
        self.assertEqual(
            (self.root / "usr/lib/libponnet.so.0.0.0").read_text(),
            "patched library",
        )

    def test_native_profile_rejects_existing_dei_binary(self) -> None:
        """Verify that native profile rejects existing dei binary."""
        self.config["CFG_ONU_MIB_PROFILE"] = "native_1u"
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("separately guarded", r.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_preserved_native_uni_rejects_existing_dei_binary(self) -> None:
        """Check the selected UNI/library pair even when its MIB profile is preserved."""
        for selected in ("/etc/mibs/prx300_1U.ini", "/ptconf/custom.ini"):
            for instance in ("0x0101", "0x101", "257"):
                with self.subTest(selected=selected, instance=instance):
                    self.put(selected.lstrip("/"), f"11 {instance} 0 0\n")
                    self.env_values["mib_file"] = selected
                    self.save_env()
                    before = self.snapshot()
                    for operation in ("setup_check", "setup_apply"):
                        result = self.run_engine(operation)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("Native UNI 0x0101 is incompatible", result.stderr)
                        self.assertIn("separately guarded", result.stderr)
                        self.assertEqual(self.snapshot(), before)

    def test_patched_library_requires_supported_preserved_uni_layout(self) -> None:
        """Report unfamiliar layouts as unverified without assuming universal incompatibility."""
        for layout in ("11 0x0206 0\n", "? 11 0x0205 0\n", "11 0x0205 0\n11 0x0206 0\n", "# no UNI\n"):
            with self.subTest(layout=layout):
                self.put("ptconf/8311/dei.ini", layout)
                before = self.snapshot()
                for operation in ("setup_check", "setup_apply"):
                    result = self.run_engine(operation)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("selected MIB layout is unverified", result.stderr)
                    self.assertNotIn("is incompatible", result.stderr)
                    self.assertEqual(self.snapshot(), before)

    def test_native_profile_rejects_a_modified_non_native_uni(self) -> None:
        """Do not infer native 1U compatibility merely from a non-0x0205 instance."""
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        self.put("etc/mibs/prx300_1U.ini", "11 0x0206 0\n")
        self.config["CFG_ONU_MIB_PROFILE"] = "native_1u"
        before = self.snapshot()
        result = self.run_engine()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("supported single native UNI", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_generated_mib_matches_inherited_firmware_identity_byte_limits(self) -> None:
        """Truncate inherited fields without printing or rewriting their private fwenv values."""
        self.fresh()
        hardware = "H" * 14 + "-private-hardware-suffix"
        equipment = "E" * 20 + "-private-equipment-suffix"
        self.env_values.update({"8311_hw_ver": hardware, "8311_equipment_id": equipment, "8311_cp_hw_ver_sync": "1"})
        self.save_env()
        before = self.snapshot()
        check = self.run_engine("setup_check")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(self.snapshot(), before)
        for key, limit in (("8311_hw_ver", 14), ("8311_equipment_id", 20)):
            self.assertIn(f"inherited {key} is truncated to {limit} bytes", check.stdout)
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        applied = self.run_engine()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        mib = (self.root / "ptconf/8311/dei.ini").read_text()
        self.assertIn('256 0x0000 "ZTEG" "' + "H" * 14 + '"', mib)
        self.assertIn('6 0x0201 0 0 0 "' + "H" * 14 + '" "ZTEG"', mib)
        self.assertIn('257 0x0000 "' + "E" * 20 + '"', mib)
        self.assertNotIn("private", mib)
        self.assertEqual(self.current_env()["8311_hw_ver"], hardware)
        self.assertEqual(self.current_env()["8311_equipment_id"], equipment)
        output = check.stdout + check.stderr + applied.stdout + applied.stderr
        for secret in (hardware, equipment, "H" * 14, "E" * 20):
            self.assertNotIn(secret, output)

    def test_inherited_identity_rejects_unsafe_effective_mib_fields(self) -> None:
        """Reject control bytes and invalid vendor values before writing module state."""
        self.fresh()
        original_env = self.env_values.copy()
        for key, value in (
            ("8311_hw_ver", 'secret"value'),
            ("8311_equipment_id", "secret\\value"),
            ("8311_hw_ver", "secret\tvalue"),
            ("8311_equipment_id", "secret\rvalue"),
            ("8311_equipment_id", "secret\nvalue"),
            ("8311_hw_ver", "secret\x7fvalue"),
            ("8311_vendor_id", "TOO-LONG"),
            ("8311_vendor_id", "ZTEG\nFAKE"),
        ):
            with self.subTest(key=key, value=value):
                self.env_values = {**original_env, key: value}
                self.save_env()
                before = self.snapshot()
                result = self.run_engine()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                self.assertNotIn(value, result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_preserved_dei_mib_still_detects_required_library_patch(self) -> None:
        """Verify that preserved dei MIB still detects required library patch."""
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reboot=1 maintenance=0 config=0 patch=1", r.stdout)
        self.assertEqual((self.root / "ptconf/8311/dei.ini").read_text(), DEI)

    def test_failed_fw_readback_does_not_print_value(self) -> None:
        """Verify that failed firmware readback does not print value."""
        self.config["CFG_MANAGEMENT_IP_ADDRESS"] = "203.0.113.77"
        r = self.run_engine(FAIL_WRITE="8311_ipaddr")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Readback failed", r.stderr)
        self.assertNotIn("203.0.113.77", r.stderr)

    def test_fw_readback_rejects_matching_value_from_failed_read(self) -> None:
        """Require a successful firmware read after a write, even when stdout matches."""
        self.assertEqual(0, self.run_engine().returncode)
        self.config["CFG_MANAGEMENT_IP_ADDRESS"] = "203.0.113.77"
        result = self.run_engine(FAIL_READ="8311_ipaddr")
        self.assertEqual(self.current_env()["8311_ipaddr"], "203.0.113.77")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Readback failed for 8311_ipaddr", result.stderr)
        self.assertNotIn("203.0.113.77", result.stdout + result.stderr)

    def test_backup_rejects_read_failure_before_environment_writes(self) -> None:
        """Preserve an unreadable original rather than recording a false absence."""
        self.config["CFG_MANAGEMENT_IP_ADDRESS"] = "203.0.113.77"
        result = self.run_engine(FAIL_READ="8311_ipaddr")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Cannot capture the original state of 8311_ipaddr", result.stderr)
        self.assertEqual(self.env_values, self.current_env())
        self.assertFalse((self.root / "ptconf/8311/setup-state/fwenv.originals").exists())

    def test_inherited_serial_discards_output_from_failed_firmware_read(self) -> None:
        """Require a valid successful serial read when no override is configured."""
        before = self.snapshot()
        result = self.run_engine("setup_check", FAIL_READ="8311_gpon_sn")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no valid existing serial", result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_changed_unknown_mib_is_backed_up_before_replacement(self) -> None:
        """Verify that changed unknown MIB is backed up before replacement."""
        self.fresh()
        self.put("ptconf/8311/dei.ini", "# User custom MIB\n11 0x0101 0\n")
        original = (self.root / "ptconf/8311/dei.ini").read_bytes()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        name = "mib." + hashlib.sha256(original).hexdigest() + ".original"
        self.assertEqual((self.root / "ptconf/8311/setup-state" / name).read_bytes(), original)

    def test_failed_or_invalid_checksums_preserve_original_mib_and_allow_retry(self) -> None:
        """Reject failed or malformed digest reads even when both files would give the same output."""
        self.fresh()
        original = "# private custom MIB\n11 0x0101 0\n"
        self.put("ptconf/8311/dei.ini", original)
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        before = self.snapshot()
        for output, status in (("", 0), ("z" * 64, 0), ("a" * 63, 0), ("a" * 64, 1)):
            for target in ("source", "backup"):
                with self.subTest(output=output, status=status, target=target):
                    # Preserve a real successful source read when exercising backup verification.
                    command = """
sha256sum() {
    case "$1" in
        */.mib-original.*) ;;
        *) if [ "$FAIL_HASH_TARGET" = backup ]; then command sha256sum "$@"; return $?; fi ;;
    esac
    printf '%s  %s\\n' "$FAIL_HASH_OUTPUT" "$1"
    return "$FAIL_HASH_STATUS"
}
setup_apply"""
                    result = self.run_engine(
                        command, FAIL_HASH_OUTPUT=output, FAIL_HASH_STATUS=str(status), FAIL_HASH_TARGET=target
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Cannot verify the original MIB backup", result.stderr)
                    self.assertEqual(self.snapshot(), before)
        result = self.run_engine()
        self.assertEqual(result.returncode, 0, result.stderr)
        digest = hashlib.sha256(original.encode()).hexdigest()
        backup = self.root / "ptconf/8311/setup-state" / f"mib.{digest}.original"
        self.assertEqual(original, backup.read_text())
        self.assertEqual(0o600, backup.stat().st_mode & 0o777)

    def test_empty_matching_persistence_markers_cannot_prove_maintenance_boot(self) -> None:
        """Require a recorded boot identity before accepting matching persistence markers."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        self.put("etc/dei-setup-persist", "")
        self.put("ptconf/8311/setup-state/persistence.pending", "")
        before = self.snapshot()
        result = self.run_engine()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_changed_failsafe_ownership_is_not_removed(self) -> None:
        """Verify that changed failsafe ownership is not removed."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        r = self.run_engine(
            extra='printf "patched library" > "$FAKE_ROOT/usr/lib/libponnet.so.0.0.0"\nprintf "user replacement" > "$FAKE_ROOT/ptconf/.failsafe"\nsetup_finish || exit $?'
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.root / "ptconf/.failsafe").read_text(), "user replacement")

    def test_storage_only_change_requires_and_verifies_another_boot(self) -> None:
        """Verify that storage only change requires and verifies another boot."""
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        self.env_values.update(
            mib_file="/etc/mibs/prx300_1U.ini",
            bootcmd="run ubi_init; ubi remove rootfs_data; run flash_flash",
            **{"8311_persist_root": "0"},
        )
        self.save_env()
        self.config["CFG_STORAGE_PERSISTENT_ROOT"] = "enabled"
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reboot=1 maintenance=0 config=0 patch=0", r.stdout)
        self.assertFalse((self.root / "ptconf/.failsafe").exists())
        same_boot = self.run_engine()
        self.assertIn("reboot=1 maintenance=0 config=0 patch=0", same_boot.stdout)
        self.second_boot()
        final = self.run_engine(extra="setup_finish || exit $?")
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertIn("reboot=0 maintenance=0 config=1 patch=0", final.stdout)
        self.assertFalse((self.root / "ptconf/8311/setup-state/persistence.pending").exists())

    def test_preserve_custom_mib_rejects_stale_identity_change(self) -> None:
        """Verify that preserve custom MIB rejects stale identity change."""
        self.config["CFG_ONU_HARDWARE_VERSION"] = "NEW-HARDWARE"
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stale fields", r.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_same_custom_identity_value_is_allowed_in_preserve_mode(self) -> None:
        """Verify that same custom identity value is allowed in preserve mode."""
        self.env_values["8311_equipment_id"] = "F8648P"
        self.save_env()
        self.config["CFG_ONU_EQUIPMENT_ID"] = "F8648P"
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reboot=0", r.stdout)

    def test_omcid_still_running_after_reboot_prevents_mib_write(self) -> None:
        """Verify that omcid still running after reboot prevents MIB write."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        self.put("proc/42/comm", "omcid\n")
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("maintenance=0 config=0 patch=1", r.stdout)
        self.assertFalse((self.root / "ptconf/8311/dei.ini").exists())

    def test_disagreeing_persistence_markers_fail_closed(self) -> None:
        """Verify that disagreeing persistence markers fail closed."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        self.put("etc/dei-setup-persist", "different-owner\n")
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("markers disagree", r.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_completed_explicit_profile_is_idempotent(self) -> None:
        """Verify that completed explicit profile is idempotent."""
        self.fresh()
        self.assertEqual(self.run_engine().returncode, 0)
        self.second_boot()
        result = self.run_engine(
            extra='printf "patched library" > "$FAKE_ROOT/usr/lib/libponnet.so.0.0.0"\nsetup_finish || exit $?'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.put("proc/sys/kernel/random/boot_id", "boot-three\n")
        before = self.snapshot()
        again = self.run_engine(extra="setup_finish || exit $?")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("reboot=0 maintenance=0 config=1 patch=0", again.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_patched_library_with_matching_boot_flags_but_tmpfs_is_rejected(self) -> None:
        """Verify that patched library with matching boot flags but tmpfs is rejected."""
        self.put(
            "proc/mounts",
            "tmpfs /overlay tmpfs rw 0 0\noverlay / overlay rw,lowerdir=/rom,upperdir=/overlay/upper,workdir=/overlay/work 0 0\n",
        )
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("persistent writable root backing is not verified", r.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_explicit_enabled_storage_with_unverified_backing_is_rejected(self) -> None:
        """Verify that explicit enabled storage with unverified backing is rejected."""
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        self.env_values["mib_file"] = "/etc/mibs/prx300_1U.ini"
        self.save_env()
        self.config["CFG_STORAGE_PERSISTENT_ROOT"] = "enabled"
        self.put(
            "proc/mounts",
            "tmpfs /overlay tmpfs rw 0 0\noverlay / overlay rw,lowerdir=/rom,upperdir=/overlay/upper,workdir=/overlay/work 0 0\n",
        )
        before = self.snapshot()
        r = self.run_engine("setup_check")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def make_matching_mib_symlink(self) -> None:
        # This is byte-identical to the requested generated MIB, so cmp alone
        # would return success and used to bypass the later symlink guard.
        """Replace the selected MIB with a symlink to byte-identical generated content."""
        generated = DEI.replace('"OLDD"', '"ZTEG"')
        self.put("etc/user-mib.ini", generated)
        (self.root / "ptconf/8311/dei.ini").unlink()
        (self.root / "ptconf/8311/dei.ini").symlink_to(self.root / "etc/user-mib.ini")
        self.config["CFG_ONU_MIB_PROFILE"] = "dei_0205"

    def test_generated_profile_rejects_identical_symlink_before_noop(self) -> None:
        """Verify that generated profile rejects identical symlink before no-op."""
        self.make_matching_mib_symlink()
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symlinked generated MIB", r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.root / "ptconf/8311/dei.ini").is_symlink())

    def test_mib_install_rechecks_identical_symlink_before_noop(self) -> None:
        """Verify that MIB install rechecks identical symlink before no-op."""
        self.make_matching_mib_symlink()
        before = self.snapshot()
        r = self.run_engine(
            "setup_paths; SU_PROFILE=dei_0205; SU_SERIAL=ZTEG1234ABCD; SU_MIB=/ptconf/8311/dei.ini; setup_install_mib"
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symlinked MIB", r.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_preserved_custom_mib_rejects_changed_derived_vendor_prefix(self) -> None:
        """Verify that preserved custom MIB rejects changed derived vendor prefix."""
        self.config["CFG_ONU_SERIAL"] = "ALCL1234ABCD"
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("serial vendor prefix", r.stderr)
        self.assertNotIn("ALCL1234ABCD", r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_same_serial_vendor_prefix_can_change_without_rebuilding_mib(self) -> None:
        """Verify that same serial vendor prefix can change without rebuilding MIB."""
        self.config["CFG_ONU_SERIAL"] = "ZTEG99887766"
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reboot=1 maintenance=0 config=1", r.stdout)
        self.assertEqual((self.root / "ptconf/8311/dei.ini").read_text(), DEI)

    def test_check_lists_pending_fw_keys_without_values_or_writes(self) -> None:
        """Verify that check lists pending firmware keys without values or writes."""
        self.env_values.update({"8311_omcc_version": "0xA3", "8311_fix_vlans": "1"})
        self.save_env()
        self.config.update(CFG_ONU_SERIAL="ZTEG99887766", CFG_MANAGEMENT_IP_ADDRESS="203.0.113.55")
        before = self.snapshot()
        r = self.run_engine("setup_check")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("8311_gpon_sn", "8311_omcc_version", "8311_ipaddr", "8311_fix_vlans"):
            self.assertIn(f"Setup would update {name}", r.stdout)
        self.assertNotIn("ZTEG99887766", r.stdout + r.stderr)
        self.assertNotIn("203.0.113.55", r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_owned_failsafe_uses_reboot_stable_matching_token(self) -> None:
        """Verify that owned failsafe uses reboot stable matching token."""
        self.fresh()
        r = self.run_engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        token = (self.root / "ptconf/.failsafe").read_text()
        self.assertTrue(token.startswith("dei-setup-failsafe-v1:"))
        self.assertEqual(token, (self.root / "ptconf/8311/setup-state/failsafe.owner").read_text())
        self.assertEqual((self.root / "ptconf/.failsafe").stat().st_mode & 0o777, 0o600)

    def test_explicit_native_profile_aligns_both_mib_selectors(self) -> None:
        """Verify that explicit native profile aligns both MIB selectors."""
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        self.env_values["8311_mib_file"] = "/etc/mibs/prx300_1V_bell.ini"
        self.save_env()
        self.config["CFG_ONU_MIB_PROFILE"] = "native_1u"
        check = self.run_engine("setup_check")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("Setup would update mib_file", check.stdout)
        self.assertIn("Setup would update 8311_mib_file", check.stdout)
        applied = self.run_engine()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(self.current_env()["mib_file"], "/etc/mibs/prx300_1U.ini")
        self.assertEqual(self.current_env()["8311_mib_file"], "/etc/mibs/prx300_1U.ini")
        backup = self.root / "ptconf/8311/setup-state/fwenv.originals/8311_mib_file.value"
        self.assertEqual(backup.read_text(), "/etc/mibs/prx300_1V_bell.ini")

    def test_preserved_native_mib_rejects_identity_update_target_mismatch(self) -> None:
        """Verify that preserved native MIB rejects identity update target mismatch."""
        self.put("usr/lib/libponnet.so.0.0.0", "stock library")
        self.env_values.update(mib_file="/etc/mibs/prx300_1U.ini", **{"8311_mib_file": "/etc/mibs/prx300_1V_bell.ini"})
        self.save_env()
        self.config["CFG_ONU_HARDWARE_VERSION"] = "NEW-HW"
        before = self.snapshot()
        r = self.run_engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("identity-update target", r.stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
