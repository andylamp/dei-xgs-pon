#!/usr/bin/env python3
"""Offline libponnet patch tests using synthetic bytes, never vendor binaries."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from paths import BUSYBOX, SOURCE

OFFSET = 236395
STOCK_MD5 = "5326f83e22a18929b830adfb82cfc483"
PATCHED_MD5 = "5ddb892fbf0b648479f605d9fddda50c"
MAPPING_FAILURE = "pon_net_ifname_get() failed with -12"
UNI_FAILURE = 'Can\'t create Managed Entity "PPTP Ethernet UNI" 11@517'
PERSISTENT_MOUNTS = (
    "/dev/ubi0_2 /overlay ubifs rw,relatime 0 0\n"
    "overlay / overlay rw,relatime,lowerdir=/rom,upperdir=/overlay/upper,workdir=/overlay/work 0 0\n"
)


class LibraryMockState(TypedDict):
    """Describe the fake firmware environment and recorded library-command calls."""

    fwenv: dict[str, str]
    calls: list[list[str]]
    mock_errors: NotRequired[list[str]]
    pon_state: NotRequired[int]
    fail_env_read: NotRequired[str]
    fail_staged_hash: NotRequired[bool]
    pon_error: NotRequired[bool]
    pon_previous: NotRequired[int]
    pon_time_curr: NotRequired[int]
    pon_reply: NotRequired[str]
    pon_delay: NotRequired[int]
    inherited_descriptors: NotRequired[list[int]]


def synthetic_library() -> bytes:
    """Build synthetic bytes with the stock instruction at the real patch offset.

    Returns:
        Synthetic library bytes containing no vendor executable content.

    """
    data = bytearray((index * 37 + 11) % 256 for index in range(260000))
    marker = b"SYNTHETIC LIBPONNET TEST FIXTURE!!"
    data[: len(marker)] = marker
    data[OFFSET - 3 : OFFSET + 1] = b"\x26\x10\xff\xff"
    return bytes(data)


STOCK_BYTES = synthetic_library()
PATCHED_BYTES = STOCK_BYTES[:OFFSET] + b"\xfb" + STOCK_BYTES[OFFSET + 1 :]

MOCK_PROGRAM = r"""#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import sys
import time

name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ["LIBPATCH_MOCK_STATE"])
state = json.loads(state_path.read_text())
state.setdefault("calls", []).append([name, *args])
output = ""
code = 0
try:
    if name == "md5sum":
        stock = Path(os.environ["LIBPATCH_STOCK_FIXTURE"]).read_bytes()
        patched = stock[:236395] + b"\xfb" + stock[236396:]
        for name_arg in args:
            if name_arg == "--":
                continue
            if not Path(name_arg).is_file():
                code = 1
                continue
            data = Path(name_arg).read_bytes()
            if data == stock:
                digest = "5326f83e22a18929b830adfb82cfc483"
            elif data == patched:
                digest = "5ddb892fbf0b648479f605d9fddda50c"
            else:
                digest = hashlib.md5(data).hexdigest()
            output += digest + "  " + name_arg + "\n"
            if state.get("fail_staged_hash") and Path(name_arg).name.startswith(".dei835-libponnet."):
                code = 1
    elif name == "fw_printenv":
        assert len(args) == 2 and args[0] == "-n", args
        if args[1] in state.get("fwenv", {}):
            output = str(state["fwenv"][args[1]]) + "\n"
            if args[1] == state.get("fail_env_read"):
                code = 1
        else:
            code = 1
    elif name == "pidof":
        assert args == ["omcid"], args
        if state.get("omcid_running"):
            output = "1234\n"
        else:
            code = 1
    elif name == "pon":
        assert args == ["psg"], args
        state["inherited_descriptors"] = [fd for fd in (7, 8, 9) if Path("/proc/self/fd/%d" % fd).exists()]
        if state.get("pon_delay"):
            state_path.with_name("pon-started").touch()
            time.sleep(state["pon_delay"])
        if state.get("pon_error"):
            output = "errorcode=-1\n"
            code = 1
        elif "pon_reply" in state:
            output = state["pon_reply"]
        else:
            output = "errorcode=0 current=%s previous=%s time_curr=%s\n" % (state.get("pon_state", 11), state.get("pon_previous", 40), state.get("pon_time_curr", 20))
    else:
        raise AssertionError("Unexpected mock command: " + name)
except Exception as error:
    state.setdefault("mock_errors", []).append(repr(error))
    output = "MOCK ERROR: %r\n" % error
    code = 97
state_path.write_text(json.dumps(state))
sys.stdout.write(output)
sys.exit(code)
"""


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class LibponnetTests(unittest.TestCase):
    """Exercise guarded binary patching and restoration against an isolated module."""

    def setUp(self) -> None:
        """Create isolated module files and replace device commands with controlled fakes."""
        self.temporary = tempfile.TemporaryDirectory(prefix="dei835-libpatch-test-")
        self.root = Path(self.temporary.name)
        self.module = self.root / "module"
        self.conf = self.module / "ptconf" / "8311"
        self.run_dir = self.root / "run"
        self.bin = self.root / "bin"
        self.library = self.module / "usr" / "lib" / "libponnet.so.0.0.0"
        self.backup = self.conf / "dei835-libponnet.stock"
        self.owner = self.conf / "dei835-libponnet.owner"
        self.failsafe = self.module / "ptconf" / ".failsafe"
        self.mib = self.conf / "dei.ini"
        self.mounts = self.module / "proc" / "mounts"
        for path in (self.conf, self.bin, self.library.parent, self.mounts.parent):
            path.mkdir(parents=True)
        self.library.write_bytes(STOCK_BYTES)
        self.library.chmod(0o640)
        self.failsafe.touch()
        self.mounts.write_text(PERSISTENT_MOUNTS)
        self.mib.write_text("# synthetic MIB\n11 0x0205 0 0 0\n5 0x0201 0\n6 0x0201 0\n")
        self.fixture_path = self.root / "synthetic-stock.bin"
        self.fixture_path.write_bytes(STOCK_BYTES)
        self.state_path = self.root / "state.json"
        self.state: LibraryMockState = {
            "fwenv": {
                "mib_file": "/ptconf/8311/dei.ini",
                "8311_persist_root": "1",
                "bootcmd": "run ubi_init; run flash_flash",
            },
            "calls": [],
        }
        program = self.bin / "mock_libpatch.py"
        program.write_text(MOCK_PROGRAM)
        program.chmod(0o755)
        for name in ("md5sum", "fw_printenv", "pidof", "pon"):
            (self.bin / name).symlink_to(program)
        self.pon_program = self.module / "usr/bin/pon"
        self.pon_program.parent.mkdir(parents=True)
        self.pon_program.write_text(MOCK_PROGRAM)
        self.pon_program.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": os.pathsep.join((
                str(self.pon_program.parent),
                str(self.bin),
                os.environ.get("PATH", "/usr/bin:/bin"),
            )),
            "DEI835_MODULE_ROOT": str(self.module),
            "DEI835_CONF_DIR": str(self.conf),
            "LIBPATCH_TEST_RUN": str(self.run_dir),
            "LIBPATCH_MOCK_BIN": str(self.bin),
            "LIBPATCH_MOCK_STATE": str(self.state_path),
            "LIBPATCH_STOCK_FIXTURE": str(self.fixture_path),
            "CFG_LIBRARY_PATCH_ENABLED": "true",
            "CFG_LIBRARY_PATCH_POLICY": "auto",
        }

    def tearDown(self) -> None:
        """Remove the temporary filesystem created for this test."""
        self.temporary.cleanup()

    def invoke(
        self, function: str = "lp_assess", *, mock_pon: bool = True, lock_fds: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run one binary-maintenance function against the fake module.

        Args:
            function: Sourced shell function to invoke.
            mock_pon: Install the explicit PON mock function before sourcing the library.
            lock_fds: Open the installer and worker descriptor slots before the call.

        Returns:
            Captured process result after refreshing the recorded mock state.

        """
        self.state_path.write_text(json.dumps(self.state))
        pon_definition = 'pon() { "$LIBPATCH_MOCK_BIN/pon" "$@"; }\n' if mock_pon else ""
        lock_definition = (
            'exec 7>"$LIBPATCH_MOCK_STATE.fd7" 8>"$LIBPATCH_MOCK_STATE.fd8" 9>"$LIBPATCH_MOCK_STATE.fd9"\n'
            if lock_fds
            else ""
        )
        launcher = (
            pon_definition
            + lock_definition
            + r"""
md5sum() { "$LIBPATCH_MOCK_BIN/md5sum" "$@"; }
fw_printenv() { "$LIBPATCH_MOCK_BIN/fw_printenv" "$@"; }
pidof() { "$LIBPATCH_MOCK_BIN/pidof" "$@"; }
. "${1%/*}/checksums.sh" || exit 91
D=$DEI835_CONF_DIR
R=$LIBPATCH_TEST_RUN
. "$1" || exit 91
"$2"
result=$?
printf 'TEST_STATE=%s TEST_APPLIED=%s\n' "${LP_STATE:-unset}" "${LP_APPLIED:-unset}"
printf 'TEST_RECOMMENDATION=%s TEST_EVIDENCE=%s TEST_FILES=%s TEST_CONTEXT=%s\n' "${LP_RECOMMENDATION:-unset}" "${LP_LOG_EVIDENCE:-unset}" "${LP_LOG_FILES:-unset}" "${LP_PON_CONTEXT:-unset}"
printf 'TEST_CURRENT=%s TEST_PREVIOUS=%s TEST_TIME_CURR=%s TEST_MIB_STATUS=%s TEST_RESET=%s TEST_LIMIT=%s\n' "${LP_PON_CURRENT:-unset}" "${LP_PON_PREVIOUS:-unset}" "${LP_PON_TIME_CURR:-unset}" "${LP_MIB_STATUS:-unset}" "${LP_LOG_RESET_MATCHES:-unset}" "${LP_LOG_LIMIT_REACHED:-unset}"
exit "$result"
"""
        )
        result = subprocess.run(
            [BUSYBOX, "ash", "-c", launcher, "offline-libpatch", str(SOURCE / "libponnet-patch.sh"), function],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.state = cast("LibraryMockState", json.loads(self.state_path.read_text()))
        self.assertEqual([], self.state.get("mock_errors", []), result.stdout + result.stderr)
        return result

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        """Require a captured command to finish successfully.

        Args:
            result: Captured shell process whose output explains any failure.

        """
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def assert_no_binary_changes(self, before: bytes = STOCK_BYTES) -> None:
        """Require unchanged library bytes and no newly created patch state.

        Args:
            before: Expected contents of the active library.

        """
        self.assertEqual(before, self.library.read_bytes())
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.owner.exists())
        self.assertEqual([], list(self.library.parent.glob(".dei835-libponnet.*")))

    def write_log(self, contents: str, *, relative: str = "tmp/omcid.log") -> Path:
        """Create a supported log fixture beneath the isolated module root.

        Args:
            contents: Synthetic log entries to inspect.
            relative: Module-relative location of the log fixture.

        Returns:
            The created log path.

        """
        path = self.module / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    def test_patch_requires_explicit_enabled_true_before_creating_state(self) -> None:
        """Reject absent, false, or malformed opt-in values without creating patch state."""
        for enabled in (None, "false", "1", "yes", "TRUE"):
            with self.subTest(enabled=enabled):
                if enabled is None:
                    self.env.pop("CFG_LIBRARY_PATCH_ENABLED", None)
                else:
                    self.env["CFG_LIBRARY_PATCH_ENABLED"] = enabled
                result = self.invoke("lp_patch")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("[library_patch] enabled = true", result.stderr)
                self.assertIn("rerun install --config PATH", result.stderr)
                self.assert_no_binary_changes()
                self.assertFalse(self.run_dir.exists())

    def test_opt_in_does_not_override_never_or_missing_policy(self) -> None:
        """Require both explicit opt-in and an automatic patch policy."""
        for policy in (None, "never", "invalid"):
            with self.subTest(policy=policy):
                if policy is None:
                    self.env.pop("CFG_LIBRARY_PATCH_POLICY", None)
                else:
                    self.env["CFG_LIBRARY_PATCH_POLICY"] = policy
                result = self.invoke("lp_patch")
                self.assertNotEqual(0, result.returncode)
                self.assert_no_binary_changes()
                self.assertFalse(self.run_dir.exists())

    def test_already_patched_library_needs_no_opt_in_for_noop(self) -> None:
        """Leave an already patched library unchanged when the opt-in is disabled."""
        self.library.write_bytes(PATCHED_BYTES)
        self.env.pop("CFG_LIBRARY_PATCH_ENABLED")
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assert_no_binary_changes(PATCHED_BYTES)
        self.assertIn("TEST_APPLIED=0", result.stdout)

    def test_o5_alone_does_not_suggest_a_patch(self) -> None:
        """Treat O5 as context without inferring a mapping defect."""
        self.state["pon_state"] = 51
        self.mib.write_text("11 0x0101 0\n")
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_CONTEXT=O5", result.stdout)
        self.assertIn("TEST_RECOMMENDATION=not-indicated", result.stdout)
        self.assertIn("log diagnostics unavailable", result.stdout)
        self.assert_no_binary_changes()

    def test_specific_log_evidence_is_sanitized_and_never_changes_eligibility(self) -> None:
        """Recommend reviewing a matching log signature without exposing its private text."""
        private_text = "subscriber-value-for-redaction-test"
        log = self.write_log(f"{MAPPING_FAILURE} {private_text}\n{UNI_FAILURE} {private_text}\n")
        before = log.read_bytes()
        self.mib.write_text("11 0x0101 0\n")
        self.state["pon_state"] = 51
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_STATE=not-applicable", result.stdout)
        self.assertIn("TEST_RECOMMENDATION=suggested TEST_EVIDENCE=uni-mapping", result.stdout)
        self.assertIn("mapping-errors=1; UNI-517-creation-errors=1", result.stdout)
        self.assertIn('mib_profile = "dei_0205"', result.stdout)
        self.assertNotIn(private_text, result.stdout + result.stderr)
        self.assertNotIn(MAPPING_FAILURE, result.stdout + result.stderr)
        self.assertNotIn(UNI_FAILURE, result.stdout + result.stderr)
        self.assertEqual(before, log.read_bytes())
        self.assert_no_binary_changes()
        self.assertFalse(self.run_dir.exists())

    def test_specific_uni_creation_failure_alone_is_an_advisory(self) -> None:
        """Recognize the exact failing UNI instance without requiring a second log line."""
        self.mib.write_text("11 0x0101 0\n")
        self.write_log(UNI_FAILURE + "\n", relative="var/log/omcid.log")
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_RECOMMENDATION=suggested TEST_EVIDENCE=uni-mapping TEST_FILES=1", result.stdout)

    def test_generic_or_other_uni_errors_do_not_suggest_a_patch(self) -> None:
        """Reject generic reset, encryption, mapping-only, and other-UNI hints as proof."""
        self.mib.write_text("11 0x0101 0\n")
        cases = (
            "MIB reset failed\n",
            "key_errors=1234\n",
            MAPPING_FAILURE + "\n",
            UNI_FAILURE.replace("11@517", "11@257") + "\n",
            UNI_FAILURE.replace("11@517", "11@5170") + "\n",
        )
        for contents in cases:
            with self.subTest(contents=contents):
                self.write_log(contents)
                result = self.invoke()
                self.assert_success(result)
                self.assertIn("TEST_RECOMMENDATION=not-indicated TEST_EVIDENCE=none", result.stdout)
                self.assert_no_binary_changes()

    def test_historical_log_matches_are_not_claimed_as_current_failures(self) -> None:
        """Label old log evidence as potentially historical without trusting clock timestamps."""
        self.mib.write_text("11 0x0101 0\n")
        log = self.write_log("2001-01-01 " + UNI_FAILURE + "\n")
        os.utime(log, (1, 1))
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("may be historical", result.stdout)
        self.assertIn("not proof of a current fault", result.stdout)
        self.assertIn("TEST_RECOMMENDATION=suggested", result.stdout)
        self.assert_no_binary_changes()

    def test_patched_library_does_not_recommend_repatching_from_old_logs(self) -> None:
        """Keep historical creation failures from recommending a second patch."""
        self.library.write_bytes(PATCHED_BYTES)
        self.write_log(UNI_FAILURE + "\n")
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_RECOMMENDATION=not-indicated TEST_EVIDENCE=uni-mapping", result.stdout)
        self.assert_no_binary_changes(PATCHED_BYTES)

    def test_unknown_library_keeps_log_hint_separate_from_patch_recommendation(self) -> None:
        """Report the specific log hint without authorizing a patch to an unknown image."""
        unknown = b"unsupported library image"
        self.library.write_bytes(unknown)
        self.write_log(UNI_FAILURE + "\n")
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("TEST_RECOMMENDATION=unavailable TEST_EVIDENCE=uni-mapping", result.stdout)
        self.assert_no_binary_changes(unknown)

    def test_log_hint_cannot_bypass_selected_mib_guard(self) -> None:
        """Leave a native-UNI image unchanged even when advisory log evidence matches."""
        self.mib.write_text("11 0x0101 0\n")
        self.write_log(UNI_FAILURE + "\n")
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assertIn("TEST_STATE=not-applicable TEST_APPLIED=0", result.stdout)
        self.assert_no_binary_changes()
        self.assertFalse(self.run_dir.exists())

    def test_rotated_plaintext_logs_remain_advisory(self) -> None:
        """Inspect rotated plaintext logs without claiming their errors are current."""
        self.mib.write_text("11 0x0101 0\n")
        self.write_log(UNI_FAILURE + "\n", relative="tmp/omcid.log.old")
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_RECOMMENDATION=suggested TEST_EVIDENCE=uni-mapping TEST_FILES=1", result.stdout)
        self.assertIn("may be historical", result.stdout)

    def test_log_scan_is_bounded_by_bytes_and_lines(self) -> None:
        """Exclude signatures outside either bounded tail limit."""
        self.mib.write_text("11 0x0101 0\n")
        for trailing in ("ordinary status\n" * 1100, "x" * 300000 + "\n"):
            with self.subTest(trailing_size=len(trailing)):
                self.write_log(UNI_FAILURE + "\n" + trailing)
                result = self.invoke()
                self.assert_success(result)
                self.assertIn("TEST_RECOMMENDATION=not-indicated TEST_EVIDENCE=none", result.stdout)

    def test_log_symlink_cannot_escape_the_fake_module_root(self) -> None:
        """Refuse a candidate log that resolves outside the selected module filesystem."""
        self.mib.write_text("11 0x0101 0\n")
        outside = self.root / "outside.log"
        outside.write_text(UNI_FAILURE + "\n")
        log = self.module / "tmp/omcid.log"
        log.parent.mkdir()
        log.symlink_to(outside)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_EVIDENCE=none TEST_FILES=0", result.stdout)

    def test_log_aliases_are_counted_once(self) -> None:
        """Avoid duplicate signature counts when supported paths alias the same log."""
        log = self.write_log(UNI_FAILURE + "\n")
        alias = self.module / "var/log/omcid.log"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(log)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("files=1; mapping-errors=0; UNI-517-creation-errors=1", result.stdout)

    def test_failed_pon_read_is_unavailable_context(self) -> None:
        """Treat a failed PON status read as unavailable without affecting compatibility."""
        self.state["pon_error"] = True
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_STATE=needed", result.stdout)
        self.assertIn("TEST_CONTEXT=unavailable", result.stdout)

    def test_fake_module_does_not_execute_an_external_host_pon_command(self) -> None:
        """Avoid running an unrelated host utility named pon during offline assessment."""
        self.pon_program.unlink()
        result = self.invoke(mock_pon=False)
        self.assert_success(result)
        self.assertIn("TEST_CONTEXT=unavailable", result.stdout)
        self.assertNotIn("pon", [call[0] for call in self.state["calls"]])

    def test_fake_module_can_use_a_command_within_its_own_root(self) -> None:
        """Allow isolated module-root PON executables without falling back to host tools."""
        program = self.module / "usr/sbin/pon"
        program.parent.mkdir(parents=True)
        program.write_text(MOCK_PROGRAM)
        program.chmod(0o755)
        self.env["PATH"] = str(program.parent) + os.pathsep + self.env["PATH"]
        self.state["pon_state"] = 51
        result = self.invoke(mock_pon=False)
        self.assert_success(result)
        self.assertIn("TEST_CONTEXT=O5", result.stdout)
        self.assertIn("pon", [call[0] for call in self.state["calls"]])

    def test_in_root_pon_symlink_retains_its_invocation_name(self) -> None:
        """Validate the resolved command while preserving argv zero for a multicall target."""
        target = self.library.parent / "pon-test-driver"
        self.pon_program.rename(target)
        self.pon_program.symlink_to(target)
        self.state["pon_state"] = 51
        result = self.invoke(mock_pon=False)
        self.assert_success(result)
        self.assertIn("TEST_CONTEXT=O5", result.stdout)
        self.assertIn("pon", [call[0] for call in self.state["calls"]])

    def test_log_discovery_finds_plaintext_under_all_supported_roots(self) -> None:
        """Find matching nested log names beneath tmp, var, and persistent configuration."""
        self.mib.write_text("11 0x0101 0\n")
        for name in ("tmp/worker/omci.trace.log", "var/log/vendor/OMCID.LOG", "ptconf/8311/debug/omci-errors.log"):
            self.write_log(UNI_FAILURE + "\n", relative=name)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("files=3; mapping-errors=0; UNI-517-creation-errors=3", result.stdout)
        self.assertIn("TEST_RECOMMENDATION=suggested", result.stdout)
        self.assert_no_binary_changes()

    def test_log_discovery_excludes_excessive_depth(self) -> None:
        """Honor the maximum discovery depth while accepting a file at its boundary."""
        self.mib.write_text("11 0x0101 0\n")
        self.write_log(UNI_FAILURE + "\n", relative="tmp/a/b/c/d/omcid.log")
        self.write_log(UNI_FAILURE + "\n", relative="ptconf/a/b/c/omcid.log")
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_EVIDENCE=uni-mapping TEST_FILES=1", result.stdout)

    def test_log_discovery_caps_distinct_files_and_reports_incompleteness(self) -> None:
        """Limit the scan to its file budget instead of claiming exhaustive coverage."""
        for index in range(40):
            self.write_log(UNI_FAILURE + "\n", relative=f"tmp/omci-{index}.log")
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_FILES=32", result.stdout)
        self.assertIn("TEST_LIMIT=1", result.stdout)
        self.assertIn("discovery may be incomplete", result.stdout)

    def test_log_discovery_rejects_compressed_and_binary_data(self) -> None:
        """Avoid treating compressed names or control-byte data as plaintext logs."""
        self.mib.write_text("11 0x0101 0\n")
        self.write_log(UNI_FAILURE + "\n", relative="tmp/omcid.log.gz")
        binary_log = self.write_log("", relative="ptconf/8311/omci-binary.log")
        binary_log.write_bytes(b"\0" + UNI_FAILURE.encode() + b"\n")
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_EVIDENCE=none TEST_FILES=0", result.stdout)

    def test_log_discovery_deduplicates_hard_links(self) -> None:
        """Count shared log inodes once even when distinct matching paths reference them."""
        log = self.write_log(UNI_FAILURE + "\n")
        alias = self.conf / "omci-copy.log"
        alias.hardlink_to(log)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("files=1; mapping-errors=0; UNI-517-creation-errors=1", result.stdout)

    def test_log_links_must_remain_under_the_supported_log_roots(self) -> None:
        """Skip a log alias to another module area even when it stays inside the fake root."""
        self.mib.write_text("11 0x0101 0\n")
        outside = self.module / "etc/omcid.log"
        outside.parent.mkdir()
        outside.write_text(UNI_FAILURE + "\n")
        alias = self.module / "tmp/omcid.log"
        alias.parent.mkdir()
        alias.symlink_to(outside)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_EVIDENCE=none TEST_FILES=0", result.stdout)

    def test_generic_mib_reset_errors_are_counted_without_recommending_patch(self) -> None:
        """Report generic reset failures separately from the specific UNI mapping evidence."""
        self.mib.write_text("11 0x0101 0\n")
        self.write_log("MIB reset failed\nMIB reset failed\n")
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("MIB-reset-errors=2", result.stdout)
        self.assertIn("TEST_RESET=2", result.stdout)
        self.assertIn("TEST_RECOMMENDATION=not-indicated TEST_EVIDENCE=none", result.stdout)

    def test_mib_parent_traversal_cannot_escape_the_module_root(self) -> None:
        """Reject a selected MIB that would otherwise read outside the isolated filesystem."""
        outside = self.root / "outside.ini"
        outside.write_text("11 0x0205 0\n")
        self.state["fwenv"]["mib_file"] = "/../outside.ini"
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assertIn("TEST_STATE=unresolved", result.stdout)
        self.assertIn("TEST_MIB_STATUS=invalid-path", result.stdout)
        self.assertNotIn("/../outside.ini", result.stdout + result.stderr)
        self.assert_no_binary_changes()

    def test_mib_symlink_cannot_escape_the_module_root(self) -> None:
        """Reject a selected MIB alias whose canonical target escapes the module root."""
        outside = self.root / "outside.ini"
        outside.write_text("11 0x0205 0\n")
        self.mib.unlink()
        self.mib.symlink_to(outside)
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assertIn("TEST_MIB_STATUS=invalid-path", result.stdout)
        self.assert_no_binary_changes()

    def test_mib_control_bytes_are_never_echoed_into_diagnostics(self) -> None:
        """Reject raw environment data that could inject private or forged report lines."""
        private_text = "private-environment-fixture"
        for separator in ("\n", "\r", "\t", "\x1b"):
            with self.subTest(separator=repr(separator)):
                self.state["fwenv"]["mib_file"] = "/missing.ini" + separator + private_text
                result = self.invoke()
                self.assert_success(result)
                self.assertIn("TEST_MIB_STATUS=invalid-path", result.stdout)
                self.assertNotIn(private_text, result.stdout + result.stderr)
                self.assertNotIn("/missing.ini", result.stdout + result.stderr)

    def test_mib_directory_is_not_a_readable_native_file(self) -> None:
        """Classify directory selections without invoking the MIB parser on them."""
        self.state["fwenv"]["mib_file"] = "/ptconf/8311"
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_MIB_STATUS=unreadable", result.stdout)
        self.assertIn("TEST_STATE=unresolved", result.stdout)

    def test_mib_alias_to_a_control_byte_filename_is_rejected(self) -> None:
        """Reject control bytes introduced by canonical resolution rather than the selector."""
        target = self.conf / "private\nfilename.ini"
        self.mib.rename(target)
        self.mib.symlink_to(target)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_MIB_STATUS=invalid-path", result.stdout)
        self.assertNotIn("private", result.stdout + result.stderr)

    def test_printable_mib_path_is_reported_without_echo_escape_expansion(self) -> None:
        """Keep literal backslashes in a validated filename from injecting report lines."""
        target = self.conf / r"literal\nfilename.ini"
        self.mib.rename(target)
        self.state["fwenv"]["mib_file"] = "/ptconf/8311/" + target.name
        result = self.invoke()
        self.assert_success(result)
        self.assertIn(r"literal\nfilename.ini", result.stdout)
        self.assertNotIn("literal\nfilename.ini", result.stdout)

    def test_mib_alias_inside_module_reports_its_validated_canonical_path(self) -> None:
        """Preserve legitimate module-local MIB aliases without displaying raw path syntax."""
        target = self.conf / "target.ini"
        self.mib.rename(target)
        self.mib.symlink_to(target)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("Configured native MIB: /ptconf/8311/target.ini", result.stdout)
        self.assertIn("TEST_STATE=needed", result.stdout)

    def test_patched_library_and_native_uni_are_reported_as_incompatible(self) -> None:
        """Refuse a second patch and report the known native-UNI compatibility conflict."""
        self.library.write_bytes(PATCHED_BYTES)
        for identifier in ("0x0101", "0x101", "257"):
            with self.subTest(identifier=identifier):
                self.mib.write_text(f"11 {identifier} 0\n")
                result = self.invoke("lp_patch")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("TEST_STATE=incompatible", result.stdout)
                self.assertIn("incompatible with the selected native UNI 0x0101", result.stdout)
                self.assert_no_binary_changes(PATCHED_BYTES)
                self.assertFalse(self.run_dir.exists())

    def test_other_patched_layouts_are_unverified_without_automatic_restoration(self) -> None:
        """Avoid claiming incompatibility for every unrecognized UNI layout."""
        self.library.write_bytes(PATCHED_BYTES)
        self.mib.write_text("11 0x0105 0\n")
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assertIn("TEST_STATE=patched", result.stdout)
        self.assertIn("compatibility with the selected MIB is unverified", result.stdout)
        self.assert_no_binary_changes(PATCHED_BYTES)

    def test_pon_context_reports_current_previous_and_state_age(self) -> None:
        """Expose the complete validated state record for optional repeated observations."""
        self.state.update(pon_state=51, pon_previous=40, pon_time_curr=12)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_CURRENT=51 TEST_PREVIOUS=40 TEST_TIME_CURR=12", result.stdout)

    def test_pon_context_rejects_incomplete_duplicate_or_conflicting_records(self) -> None:
        """Reject malformed observations without combining fields from separate records."""
        valid = "errorcode=0 current=51 previous=40 time_curr=12"
        replies = (
            "errorcode=0 current=51\n",
            valid + " current=51\n",
            valid + " previous=40\n",
            valid + " time_curr=12\n",
            valid + "\n" + valid + "\n",
            "errorcode=0 current=51\nprevious=40 time_curr=12\n",
            valid.replace("time_curr=12", "time_curr=-1") + "\n",
            valid.replace("time_curr=12", "time_curr=1.2") + "\n",
            valid.replace("errorcode=0", "errorcode=-1") + "\n",
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.state["pon_reply"] = reply
                result = self.invoke()
                self.assert_success(result)
                self.assertIn("TEST_CONTEXT=unavailable", result.stdout)
                self.assertIn(
                    "TEST_CURRENT=unavailable TEST_PREVIOUS=unavailable TEST_TIME_CURR=unavailable", result.stdout
                )

    def test_slow_pon_command_times_out_without_holding_assessment_forever(self) -> None:
        """Bound a slow vendor status read without applying a timeout to OMCI commands."""
        self.state["pon_delay"] = 2
        self.env["DEI835_PON_TIMEOUT_SECONDS"] = "1"
        result = self.invoke()
        self.assert_success(result)
        self.assertTrue(self.state_path.with_name("pon-started").exists())
        self.assertIn("TEST_CONTEXT=unavailable", result.stdout)
        self.assert_no_binary_changes()

    def test_bounded_pon_process_does_not_inherit_installer_or_worker_descriptors(self) -> None:
        """Close all known lock descriptors before starting the timeout process tree."""
        result = self.invoke("lp_pon_context", lock_fds=True)
        self.assert_success(result)
        self.assertIn("TEST_CONTEXT=non-O5", result.stdout)
        self.assertEqual([], self.state["inherited_descriptors"])

    def test_invalid_pon_timeout_never_runs_the_vendor_command(self) -> None:
        """Reject overrides that could disable or corrupt the bounded PON read."""
        for timeout in ("0", "-1", "0.1", "31", "invalid"):
            with self.subTest(timeout=timeout):
                self.env["DEI835_PON_TIMEOUT_SECONDS"] = timeout
                result = self.invoke()
                self.assert_success(result)
                self.assertIn("TEST_CONTEXT=unavailable", result.stdout)
                self.assertNotIn("pon", [call[0] for call in self.state["calls"]])

    def test_assessment_of_known_stock_is_read_only(self) -> None:
        """Verify that assessment of known stock is read-only."""
        initial_inode = self.library.stat().st_ino
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("TEST_STATE=needed", result.stdout)
        self.assertIn("maintenance prerequisites satisfied", result.stdout)
        self.assert_no_binary_changes()
        self.assertEqual(initial_inode, self.library.stat().st_ino)
        self.assertFalse(self.run_dir.exists())

    def test_known_stock_sole_205_patches_one_byte_with_atomic_replacement(self) -> None:
        """Verify that the supported UNI-0x0205 patch preserves open original mappings."""
        before = self.library.stat()
        with self.library.open("rb") as held_original:
            result = self.invoke("lp_patch")
            self.assert_success(result)
            self.assertEqual(STOCK_BYTES, held_original.read())
        after = self.library.stat()
        self.assertIn("TEST_APPLIED=1", result.stdout)
        self.assertEqual(PATCHED_BYTES, self.library.read_bytes())
        self.assertNotEqual(before.st_ino, after.st_ino)
        self.assertEqual(before.st_mode & 0o7777, after.st_mode & 0o7777)
        self.assertEqual(before.st_uid, after.st_uid)
        self.assertEqual(before.st_gid, after.st_gid)
        self.assertEqual(STOCK_BYTES, self.backup.read_bytes())
        self.assertEqual(f"dei835-v2 {STOCK_MD5} {PATCHED_MD5}\n", self.owner.read_text())
        differing = [
            index
            for index, (old, new) in enumerate(zip(STOCK_BYTES, self.library.read_bytes(), strict=True))
            if old != new
        ]
        self.assertEqual([OFFSET], differing)
        self.assertTrue(self.failsafe.exists())
        self.assertTrue((self.run_dir / "stop").exists())
        self.assertEqual([], list(self.library.parent.glob(".dei835-libponnet.*")))

    def test_patch_is_idempotent_and_does_not_replace_verified_backup(self) -> None:
        """Verify that patch is idempotent and does not replace verified backup."""
        self.assert_success(self.invoke("lp_patch"))
        inode = self.library.stat().st_ino
        backup_inode = self.backup.stat().st_ino
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assertIn("TEST_STATE=patched TEST_APPLIED=0", result.stdout)
        self.assertEqual(inode, self.library.stat().st_ino)
        self.assertEqual(backup_inode, self.backup.stat().st_ino)

    def test_preexisting_known_patched_library_is_skipped_without_claiming_ownership(self) -> None:
        """Verify that preexisting known patched library is skipped without claiming ownership."""
        self.library.write_bytes(PATCHED_BYTES)
        self.failsafe.unlink()
        inode = self.library.stat().st_ino
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assertIn("TEST_STATE=patched TEST_APPLIED=0", result.stdout)
        self.assert_no_binary_changes(PATCHED_BYTES)
        self.assertEqual(inode, self.library.stat().st_ino)
        self.assertFalse(self.run_dir.exists())

    def test_other_or_missing_ethernet_uni_is_not_applicable(self) -> None:
        """Verify that other or missing ethernet UNI is not applicable."""
        for content in ("11 0x0101 0\n", "11 257 0\n", "329 0x0101 0\n", "# 11 0x0205 0\n"):
            with self.subTest(content=content):
                self.mib.write_text(content)
                result = self.invoke("lp_patch")
                self.assert_success(result)
                self.assertIn("TEST_STATE=not-applicable TEST_APPLIED=0", result.stdout)
                self.assert_no_binary_changes()

    def test_multiple_uni_layouts_never_authorize_patch(self) -> None:
        """Verify that multiple UNI layouts never authorize patch."""
        for content in ("11 0x0205 0\n11 0x0101 0\n", "11 0x0205 0\n11 0x0205 0\n", "! 11 0x0205 0\n? 11 0x0101 0\n"):
            with self.subTest(content=content):
                self.mib.write_text(content)
                result = self.invoke("lp_patch")
                self.assert_success(result)
                self.assertIn("UNI layout: multiple-UNIs", result.stdout)
                self.assertIn("TEST_STATE=unresolved TEST_APPLIED=0", result.stdout)
                self.assert_no_binary_changes()

    def test_single_conditional_uni_never_authorizes_patch(self) -> None:
        """Verify that single conditional UNI never authorizes patch."""
        for content in ("! 11 0x0205 0\n", "? 11 0x0205 0\n"):
            with self.subTest(content=content):
                self.mib.write_text(content)
                result = self.invoke("lp_patch")
                self.assert_success(result)
                self.assertIn("UNI layout: conditional-UNI", result.stdout)
                self.assertIn("TEST_STATE=unresolved TEST_APPLIED=0", result.stdout)
                self.assert_no_binary_changes()

    def test_no_explicit_native_mib_never_guesses_from_other_setting(self) -> None:
        """Verify that no explicit native MIB never guesses from other setting."""
        del self.state["fwenv"]["mib_file"]
        self.state["fwenv"]["8311_mib_file"] = "/ptconf/8311/dei.ini"
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assertIn("TEST_STATE=unresolved TEST_APPLIED=0", result.stdout)
        self.assert_no_binary_changes()
        names_read = [call[2] for call in self.state["calls"] if call[0] == "fw_printenv"]
        self.assertNotIn("8311_mib_file", names_read)

    def test_unreadable_or_relative_mib_selection_is_unresolved(self) -> None:
        """Verify that unreadable or relative MIB selection is unresolved."""
        for selection in ("/missing.ini", "dei.ini", ""):
            with self.subTest(selection=selection):
                self.state["fwenv"]["mib_file"] = selection
                result = self.invoke("lp_patch")
                self.assert_success(result)
                self.assertIn("TEST_STATE=unresolved TEST_APPLIED=0", result.stdout)
                self.assert_no_binary_changes()

    def test_unknown_library_is_refused_even_with_expected_patch_byte(self) -> None:
        """Verify that unknown library is refused even with expected patch byte."""
        unknown = b"different build" + STOCK_BYTES[15:]
        self.library.write_bytes(unknown)
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("TEST_STATE=unknown", result.stdout)
        self.assert_no_binary_changes(unknown)

    def test_missing_library_is_refused(self) -> None:
        """Verify that missing library is refused."""
        self.library.unlink()
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("TEST_STATE=missing", result.stdout)
        self.assertFalse(self.library.exists())
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.owner.exists())

    def test_failed_staged_checksum_with_plausible_output_blocks_patch(self) -> None:
        """Reject a digest printed by a verifier that also reports a read failure."""
        self.state["fail_staged_hash"] = True
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("stock library changed or staging failed", result.stderr)
        self.assert_no_binary_changes()

    def test_missing_failsafe_refuses_patch(self) -> None:
        """Verify that missing failsafe refuses patch."""
        self.failsafe.unlink()
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("failsafe marker is absent", result.stdout + result.stderr)
        self.assert_no_binary_changes()

    def test_running_omcid_or_startup_script_refuses_patch(self) -> None:
        """Verify that running omcid or startup script refuses patch."""
        proc = self.module / "proc" / "1234"
        proc.mkdir()
        for comm, cmdline in (
            ("omcid", b"omcid\0"),
            ("omcid.sh", b"/etc/init.d/omcid.sh\0"),
            ("sh", b"sh\0/etc/init.d/omcid.sh\0start\0"),
            ("ash", b"ash\0/etc/init.d/omcid.sh\0start\0"),
            ("busybox", b"busybox\0ash\0/etc/init.d/omcid.sh\0start\0"),
        ):
            with self.subTest(comm=comm, cmdline=cmdline):
                (proc / "comm").write_text(comm + "\n")
                (proc / "cmdline").write_bytes(cmdline)
                result = self.invoke("lp_patch")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("omcid or its startup script is running", result.stdout + result.stderr)
                self.assert_no_binary_changes()

    def test_unrelated_shell_process_does_not_block_patch(self) -> None:
        """Verify that unrelated shell process does not block patch."""
        proc = self.module / "proc" / "1234"
        proc.mkdir()
        (proc / "comm").write_text("sh\n")
        (proc / "cmdline").write_bytes(b"sh\0/tmp/other-script.sh\0")
        self.assert_success(self.invoke("lp_patch"))
        self.assertEqual(PATCHED_BYTES, self.library.read_bytes())

    def test_failed_persistence_read_with_plausible_output_blocks_patch(self) -> None:
        """Require a successful firmware read before treating persistence as enabled."""
        self.state["fail_env_read"] = "8311_persist_root"
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("8311_persist_root is not 1", result.stdout + result.stderr)
        self.assert_no_binary_changes()

    def test_persistence_environment_must_match_exactly(self) -> None:
        """Verify that persistence environment must match exactly."""
        original = dict(self.state["fwenv"])
        cases = (
            {"8311_persist_root": "0"},
            {"8311_persist_root": ""},
            {"bootcmd": "run ubi_init; ubi remove rootfs_data; run flash_flash"},
            {"bootcmd": "run ubi_init; run flash_flash; custom_command"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.state["fwenv"] = {**original, **changes}
                result = self.invoke("lp_patch")
                self.assertNotEqual(0, result.returncode)
                self.assert_no_binary_changes()

    def test_nonwritable_or_nonoverlay_root_refuses_patch(self) -> None:
        """Verify that nonwritable or nonoverlay root refuses patch."""
        for mounts in (
            "overlay / overlay ro,relatime 0 0\n",
            "/dev/root / squashfs ro 0 0\n",
            "overlay /overlay overlay rw 0 0\n",
            "",
        ):
            with self.subTest(mounts=mounts):
                self.mounts.write_text(mounts)
                result = self.invoke("lp_patch")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("persistent UBIFS upper layer is not verified", result.stdout + result.stderr)
                self.assert_no_binary_changes()

    def test_ram_backing_or_shadowed_upper_layer_refuses_patch(self) -> None:
        """Verify that RAM backing or shadowed upper layer refuses patch."""
        cases = (
            PERSISTENT_MOUNTS.replace("/overlay ubifs rw", "/overlay tmpfs rw"),
            PERSISTENT_MOUNTS.replace("/overlay ubifs rw", "/overlay ubifs ro"),
            PERSISTENT_MOUNTS + "tmpfs /overlay/upper tmpfs rw,relatime 0 0\n",
            PERSISTENT_MOUNTS.replace("upperdir=/overlay/upper", "upperdir=/tmp/root/upper"),
            PERSISTENT_MOUNTS.replace("upperdir=/overlay/upper", "upperdir=/overlay-other/upper"),
            "overlay / overlay rw,upperdir=/overlay/upper 0 0\n",
        )
        for mounts in cases:
            with self.subTest(mounts=mounts):
                self.mounts.write_text(mounts)
                result = self.invoke("lp_patch")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("persistent UBIFS upper layer is not verified", result.stdout + result.stderr)
                self.assert_no_binary_changes()

    def test_library_path_submount_refuses_patch(self) -> None:
        """Verify that library path submount refuses patch."""
        for mountpoint in ("/usr", "/usr/lib", "/usr/lib/libponnet.so.0.0.0"):
            with self.subTest(mountpoint=mountpoint):
                self.mounts.write_text(PERSISTENT_MOUNTS + f"tmpfs {mountpoint} tmpfs rw,relatime 0 0\n")
                result = self.invoke("lp_patch")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("persistent UBIFS upper layer is not verified", result.stdout + result.stderr)
                self.assert_no_binary_changes()

    def test_already_patched_ram_backing_warns_without_rewriting_binary(self) -> None:
        """Verify that already patched RAM backing warns without rewriting binary."""
        self.library.write_bytes(PATCHED_BYTES)
        inode = self.library.stat().st_ino
        self.mounts.write_text(PERSISTENT_MOUNTS.replace("/overlay ubifs rw", "/overlay tmpfs rw"))
        result = self.invoke("lp_patch")
        self.assert_success(result)
        self.assertIn("TEST_STATE=patched TEST_APPLIED=0", result.stdout)
        self.assertIn("WARNING: binary persistence is unverified", result.stdout)
        self.assert_no_binary_changes(PATCHED_BYTES)
        self.assertEqual(inode, self.library.stat().st_ino)

    def test_library_symlink_refuses_patch_without_touching_target(self) -> None:
        """Verify that library symlink refuses patch without touching target."""
        target = self.library.with_name("user-library.bin")
        self.library.rename(target)
        self.library.symlink_to(target)
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("target library is a symlink", result.stdout + result.stderr)
        self.assertTrue(self.library.is_symlink())
        self.assertEqual(STOCK_BYTES, target.read_bytes())
        self.assert_no_binary_changes()

    def test_conflicting_backup_is_preserved_and_blocks_patch(self) -> None:
        """Verify that conflicting backup is preserved and blocks patch."""
        self.backup.write_bytes(b"user backup from another build")
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("existing libponnet backup", result.stderr)
        self.assertEqual(STOCK_BYTES, self.library.read_bytes())
        self.assertEqual(b"user backup from another build", self.backup.read_bytes())
        self.assertFalse(self.owner.exists())

    def test_verified_existing_backup_is_preserved(self) -> None:
        """Verify that verified existing backup is preserved."""
        self.backup.write_bytes(STOCK_BYTES)
        inode = self.backup.stat().st_ino
        self.assert_success(self.invoke("lp_patch"))
        self.assertEqual(inode, self.backup.stat().st_ino)
        self.assertEqual(STOCK_BYTES, self.backup.read_bytes())

    def test_backup_symlink_refuses_patch_and_preserves_target(self) -> None:
        """Verify that backup symlink refuses patch and preserves target."""
        target = self.conf / "user-stock-backup.bin"
        target.write_bytes(STOCK_BYTES)
        self.backup.symlink_to(target)
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("backup/ownership paths must not be symlinks", result.stderr)
        self.assertEqual(STOCK_BYTES, self.library.read_bytes())
        self.assertTrue(self.backup.is_symlink())
        self.assertEqual(STOCK_BYTES, target.read_bytes())
        self.assertFalse(self.owner.exists())

    def test_owner_symlink_refuses_patch_even_with_valid_marker_content(self) -> None:
        """Verify that owner symlink refuses patch even with valid marker content."""
        target = self.conf / "user-owner.txt"
        content = f"dei835-v2 {STOCK_MD5} {PATCHED_MD5}\n"
        target.write_text(content)
        self.owner.symlink_to(target)
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("backup/ownership paths must not be symlinks", result.stderr)
        self.assertEqual(STOCK_BYTES, self.library.read_bytes())
        self.assertTrue(self.owner.is_symlink())
        self.assertEqual(content, target.read_text())
        self.assertFalse(self.backup.exists())

    def test_foreign_owner_marker_blocks_patch(self) -> None:
        """Verify that foreign owner marker blocks patch."""
        self.owner.write_text("owned by another installer\n")
        result = self.invoke("lp_patch")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unknown libponnet ownership marker", result.stderr)
        self.assertEqual(STOCK_BYTES, self.library.read_bytes())
        self.assertEqual("owned by another installer\n", self.owner.read_text())
        self.assertFalse(self.backup.exists())

    def test_owned_restore_is_atomic_and_retains_verified_stock_backup(self) -> None:
        """Verify that owned restore is atomic and retains verified stock backup."""
        self.assert_success(self.invoke("lp_patch"))
        inode = self.library.stat().st_ino
        with self.library.open("rb") as held_patched:
            result = self.invoke("lp_restore")
            self.assert_success(result)
            self.assertEqual(PATCHED_BYTES, held_patched.read())
        self.assertEqual(STOCK_BYTES, self.library.read_bytes())
        self.assertNotEqual(inode, self.library.stat().st_ino)
        self.assertEqual(STOCK_BYTES, self.backup.read_bytes())
        self.assertFalse(self.owner.exists())
        self.assertTrue(self.failsafe.exists())

    def test_unowned_preexisting_patch_cannot_be_restored(self) -> None:
        """Verify that unowned preexisting patch cannot be restored."""
        self.library.write_bytes(PATCHED_BYTES)
        self.backup.write_bytes(STOCK_BYTES)
        result = self.invoke("lp_restore")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("does not own the binary patch", result.stderr)
        self.assertEqual(PATCHED_BYTES, self.library.read_bytes())

    def test_owned_restore_refuses_later_unknown_library(self) -> None:
        """Verify that owned restore refuses later unknown library."""
        self.assert_success(self.invoke("lp_patch"))
        unknown = PATCHED_BYTES + b"user modification"
        self.library.write_bytes(unknown)
        result = self.invoke("lp_restore")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("current library is unknown", result.stderr)
        self.assertEqual(unknown, self.library.read_bytes())
        self.assertTrue(self.owner.exists())

    def test_owned_restore_requires_failsafe_and_verified_backup(self) -> None:
        """Verify that owned restore requires failsafe and verified backup."""
        self.assert_success(self.invoke("lp_patch"))
        self.failsafe.unlink()
        result = self.invoke("lp_restore")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(PATCHED_BYTES, self.library.read_bytes())
        self.failsafe.touch()
        self.backup.write_bytes(b"damaged backup")
        result = self.invoke("lp_restore")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("existing libponnet backup is not the known stock file", result.stderr)
        self.assertEqual(PATCHED_BYTES, self.library.read_bytes())

    def test_owned_restore_rejects_backup_symlink(self) -> None:
        """Verify that owned restore rejects backup symlink."""
        self.assert_success(self.invoke("lp_patch"))
        target = self.conf / "user-stock-backup.bin"
        self.backup.rename(target)
        self.backup.symlink_to(target)
        result = self.invoke("lp_restore")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("backup/ownership paths must not be symlinks", result.stderr)
        self.assertEqual(PATCHED_BYTES, self.library.read_bytes())
        self.assertTrue(self.backup.is_symlink())
        self.assertEqual(STOCK_BYTES, target.read_bytes())
        self.assertTrue(self.owner.exists())


if __name__ == "__main__":
    unittest.main()
