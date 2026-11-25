"""Offline tests for the optional management-port recovery; no device access.

The vendor switch utility is replaced by a file-backed stand-in. Every scenario
uses a synthetic bridge-port table, so nothing here establishes how a real
module's switch behaves.
"""

from __future__ import annotations

import subprocess
import time
import unittest
from typing import TYPE_CHECKING

import test_installer as installer_support
import test_persistence as support
from paths import BUSYBOX, SOURCE

if TYPE_CHECKING:
    from pathlib import Path

# One record file per allocated bridge port keeps a whole-table scan fast.
SWITCH_MOCK = r"""#!/bin/sh
state=${DEI835_MOCK_SWITCH:?}
printf '%s\n' "$*" >>"${state}/calls"
fail() {
    printf '%s\n' "$*" >>"${state}/errors"
    exit 97
}
case "${1:-}" in
    GSW_BRIDGE_PORT_CONFIG_GET)
        [ "$#" = 2 ] || fail "unexpected read: $*"
        port=${2#nBridgePortId=}
        case "${port}" in '' | *[!0-9]*) fail "unexpected port: $*" ;; *) ;; esac
        if [ -e "${state}/hang" ]; then exec /bin/sleep 60; fi
        printf x >>"${state}/reads.${port}"
        count=$(wc -c <"${state}/reads.${port}")
        count=$((count + 0))
        if [ -e "${state}/at.${port}.${count}" ]; then
            mv "${state}/at.${port}.${count}" "${state}/port.${port}"
        fi
        if [ ! -f "${state}/port.${port}" ]; then
            echo 'ERROR: bridge port is not allocated' >&2
            exit 1
        fi
        if [ -e "${state}/raw.${port}" ]; then
            cat "${state}/raw.${port}"
            exit 0
        fi
        IFS='|' read -r bridge logical group map <"${state}/port.${port}"
        printf 'Returned values:\n----------------\n'
        printf '\t%s\t= %s\n' nBridgePortId "${port}" nBridgeId "${bridge}" \
            nDestLogicalPortId "${logical}" nDestSubIfIdGroup "${group}" bBridgePortMapEnable 1 \
            'nBridgePortMapIndex[0]' "${map}" 'nBridgePortMapIndex[1]' 0x0
        ;;
    GSW_BRIDGE_PORT_CONFIG_SET)
        port=${3#nBridgePortId=}
        expected="GSW_BRIDGE_PORT_CONFIG_SET bForce=1 nBridgePortId=${port} nBridgeId=0"
        expected="${expected} nDestLogicalPortId=4 nDestSubIfIdGroup=1 bEgressExtendedVlanEnable=0"
        expected="${expected} nEgressExtendedVlanBlockId=0 bBridgePortMapEnable=1 Index=0 MapValue=0x1"
        [ "$*" = "${expected}" ] || fail "unexpected write: $*"
        [ -f "${state}/port.${port}" ] || fail "write to an unallocated port: $*"
        printf '%s\n' "${port}" >>"${state}/writes"
        if [ -e "${state}/fail-writes" ]; then exit 1; fi
        if [ ! -e "${state}/ignore-writes" ]; then
            printf '0x0|0x4|0x1|0x1\n' >"${state}/port.${port}"
        fi
        ;;
    *) fail "unexpected command: $*" ;;
esac
"""

HEALTHY = "0x0|0x4|0x1|0x1"
EMPTY_MAP = "0x0|0x4|0x1|0x0"
MOVED = "0x1|0x4|0x1|0x0"
UNI = "0x1|0x4|0x0|0x200"
SERVICE = "0x1|0x2|0x6|0x18"
LCT = "6"
NOT_O5 = 40


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class ManagementRecoveryTests(unittest.TestCase):
    """Exercise the gated bridge-port restore against a synthetic switch table."""

    def setUp(self) -> None:
        """Stage the runtime, a healthy Internet service and a detached management port."""
        self.module = support.PersistenceTests("test_check_reports_actual_gems_and_makes_no_changes")
        self.module.setUp()
        self.addCleanup(self.module.tearDown)
        self.helper = self.module.conf / "dei-lct.sh"
        self.helper.write_bytes((SOURCE / "dei-lct.sh").read_bytes())
        self.switch = self.module.root / "switch"
        self.switch.mkdir()
        self.mock = self.module.bin / "switch_cli"
        self.mock.write_text(SWITCH_MOCK)
        self.mock.chmod(0o755)
        self.module.env["DEI835_MOCK_SWITCH"] = str(self.switch)
        for interface in ("eth0_0", "eth0_0_1_lct"):
            (self.module.net / interface).touch()
        self.service()
        self.ports({"3": UNI, LCT: EMPTY_MAP, "9": SERVICE})
        self.configure()

    def service(self, *, pon_state: int | None = None, events: dict[str, support.ModuleEvent] | None = None) -> None:
        """Start from a healthy Internet service, optionally without O5 or with timed events."""
        self.module.state = support.initial_state(support.NATIVE_RULE)
        for gem in ("1084", "1085", "1086", "1087"):
            self.module.state["gems"][gem]["enc"] = support.DOWNSTREAM_ENCRYPTION
        if pon_state is not None:
            self.module.state["pon_state"] = pon_state
        if events is not None:
            self.module.state["events"] = events

    def ports(self, records: dict[str, str]) -> None:
        """Replace the synthetic bridge-port table."""
        for existing in self.switch.glob("port.*"):
            existing.unlink()
        for port, record in records.items():
            (self.switch / f"port.{port}").write_text(record + "\n")

    def record(self, port: str = LCT) -> str:
        """Read one synthetic bridge-port record.

        Returns:
            The bridge, logical port, sub-interface and map fields.

        """
        return (self.switch / f"port.{port}").read_text().strip()

    def configure(self, *, recovery: bool | None = True, interval: int = 10) -> str:
        """Write a runtime configuration with or without the recovery opt-in.

        Returns:
            TOML text that can also be injected during a polling pass.

        """
        text = f"schema_version = 1\n[watcher]\ninterval_seconds = {interval}\n"
        if recovery is not None:
            text += f"[management_recovery]\nenabled = {str(recovery).lower()}\n"
        (self.module.conf / "dei-pon.toml").write_text(text)
        return text

    def idle_line(self, *, events: dict[str, support.ModuleEvent] | None = None) -> None:
        """Use a line without O5: passes are cheap and only the fallback can authorize a write.

        With a 120-second interval the second matching sample completes the fallback window.
        """
        self.configure(interval=120)
        self.service(pon_state=NOT_O5, events=events)

    def watch(self, passes: int) -> str:
        """Run a bounded number of watcher polling passes.

        Returns:
            The private runtime log written by the watcher.

        """
        log = self.module.conf / "dei835.log"
        log.unlink(missing_ok=True)
        result = self.module.invoke("--watch", passes=passes)
        self.module.assert_success(result)
        self.assertFalse((self.switch / "errors").exists(), self.lines("errors"))
        self.assertEqual([], self.module.state["mutations"])
        return log.read_text()

    def lines(self, name: str) -> list[str]:
        """Read a list recorded by the switch stand-in.

        Returns:
            Recorded lines, or an empty list when nothing was recorded.

        """
        path: Path = self.switch / name
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def writes(self) -> list[str]:
        """List the bridge ports that received a restore write.

        Returns:
            Bridge-port numbers in write order.

        """
        return self.lines("writes")

    def test_default_configuration_never_runs_the_switch_utility(self) -> None:
        """Keep every switch read and write behind the explicit opt-in."""
        for recovery in (None, False):
            with self.subTest(recovery=recovery):
                self.configure(recovery=recovery)
                self.watch(2)
                check = self.module.invoke("--check")
                self.module.assert_success(check)
                self.assertIn("Management recovery: disabled", check.stdout)
                status = self.module.invoke("", script="status.sh")
                self.module.assert_success(status)
                self.assertIn("Management recovery: disabled", status.stdout)
                self.assertEqual([], self.lines("calls"))
                self.assertEqual(EMPTY_MAP, self.record())

    def test_check_and_status_report_the_discovered_port_without_writing(self) -> None:
        """Describe the management port read-only, without logs or worker state."""
        result = self.module.invoke("--check")
        self.module.assert_success(result)
        self.assertIn("Management recovery: enabled", result.stdout)
        self.assertIn(
            "Management port: interface=eth0_0_1_lct bridge-port=6 state=detached "
            "bridge=0x0 logical-port=0x4 sub-interface=0x1 map=0x0",
            result.stdout,
        )
        self.assertFalse(self.module.run_dir.exists())
        self.assertFalse((self.module.conf / "dei835.log").exists())
        self.ports({"3": UNI, LCT: HEALTHY, "9": SERVICE})
        status = self.module.invoke("", script="status.sh")
        self.module.assert_success(status)
        self.assertIn("bridge-port=6 state=healthy", status.stdout)
        self.assertEqual([], self.writes())
        self.assertEqual([], self.module.state["mutations"])

    def test_watcher_restores_both_recorded_detached_states(self) -> None:
        """Restore the empty map, with or without the move into the service bridge."""
        for preimage in (EMPTY_MAP, MOVED):
            with self.subTest(preimage=preimage):
                self.service()
                self.ports({"3": UNI, LCT: preimage, "9": SERVICE})
                (self.switch / "writes").unlink(missing_ok=True)
                log = self.watch(2)
                self.assertEqual([LCT], self.writes())
                self.assertEqual(HEALTHY, self.record())
                self.assertEqual(UNI, self.record("3"))
                self.assertEqual(SERVICE, self.record("9"))
                self.assertIn("Restoring management port eth0_0_1_lct bridge-port=6", log)
                self.assertIn("(selected service is stable)", log)
                self.assertIn("Management port restored: eth0_0_1_lct bridge-port=6 bridge=0x0", log)

    def test_single_sample_never_authorizes_a_write(self) -> None:
        """Require the same detached record on consecutive polling passes."""
        log = self.watch(1)
        self.assertEqual([], self.writes())
        self.assertIn("waiting for a matching sample", log)

    def test_healthy_port_is_observed_without_writes_or_repeated_log_lines(self) -> None:
        """Leave a working management port alone and report it once."""
        self.idle_line()
        self.ports({"3": UNI, LCT: HEALTHY, "9": SERVICE})
        log = self.watch(5)
        self.assertEqual([], self.writes())
        self.assertEqual(1, log.count("Management port healthy: eth0_0_1_lct bridge-port=6"))
        scans = [call for call in self.lines("calls") if call.endswith("nBridgePortId=127")]
        self.assertEqual(1, len(scans))

    def test_guide_port_number_is_never_assumed(self) -> None:
        """Write the discovered port even when bridge port 4 looks detached."""
        self.ports({"4": "0x1|0x2|0x4|0x0", "11": MOVED})
        self.watch(2)
        self.assertEqual(["11"], self.writes())
        self.assertEqual("0x1|0x2|0x4|0x0", self.record("4"))
        self.assertEqual(HEALTHY, self.record("11"))

    def test_unrecognized_states_are_never_written(self) -> None:
        """Refuse any record outside the two recorded detached preimages."""
        for record in ("0x1|0x4|0x1|0x18", "0x0|0x4|0x1|0x9", "0x2|0x4|0x1|0x0", "0x1|0x4|0x1|0x1"):
            with self.subTest(record=record):
                self.idle_line()
                self.ports({"3": UNI, LCT: record, "9": SERVICE})
                log = self.watch(4)
                self.assertEqual([], self.writes())
                self.assertEqual(record, self.record())
                self.assertIn("unrecognized state", log)
                self.assertIn("no changes authorized", log)

    def test_ambiguous_or_missing_identity_stops_changes(self) -> None:
        """Stop when the table holds several LCT identities, or none."""
        cases = (
            ({"4": EMPTY_MAP, LCT: EMPTY_MAP}, "several bridge ports present the LCT identity"),
            ({"3": UNI, "9": SERVICE}, "no bridge port presents the supported LCT identity"),
            ({"4": "0x0|0x4|0x2|0x0", LCT: "0x0|0x3|0x1|0x0"}, "no bridge port presents"),
        )
        for records, message in cases:
            with self.subTest(message=message):
                self.idle_line()
                self.ports(records)
                log = self.watch(4)
                self.assertEqual([], self.writes())
                self.assertIn(message, log)
                self.assertEqual(1, log.count(message))

    def test_failed_discovery_is_not_rescanned_on_every_poll(self) -> None:
        """Limit the cost of a table scan that cannot identify the port."""
        self.ports({"3": UNI, "9": SERVICE})
        self.idle_line()
        self.watch(3)
        scans = [call for call in self.lines("calls") if call.endswith("nBridgePortId=127")]
        self.assertEqual(1, len(scans))
        (self.switch / "calls").unlink()
        self.idle_line()
        self.watch(6)
        scans = [call for call in self.lines("calls") if call.endswith("nBridgePortId=127")]
        self.assertEqual(2, len(scans))

    def test_unsupported_interface_layout_prevents_switch_access(self) -> None:
        """Require one LCT interface whose name matches the supported layout."""
        lct = self.module.net / "eth0_0_1_lct"
        parent = self.module.net / "eth0_0"
        cases: tuple[tuple[str, list[str], list[str]], ...] = (
            ("found 0", ["eth0_0_1_lct"], []),
            ("found 2", [], ["eth0_1_1_lct"]),
            ("supported sub-interface layout", ["eth0_0_1_lct"], ["eth0_0_2_lct"]),
            ("supported sub-interface layout", ["eth0_0"], []),
        )
        for message, removed, added in cases:
            with self.subTest(message=message, removed=removed):
                self.idle_line()
                lct.touch()
                parent.touch()
                for name in removed:
                    (self.module.net / name).unlink()
                for name in added:
                    (self.module.net / name).touch()
                log = self.watch(3)
                for name in added:
                    (self.module.net / name).unlink()
                self.assertIn(message, log)
                self.assertEqual([], self.lines("calls"))

    def test_failsafe_prevents_switch_reads_and_writes(self) -> None:
        """Leave the switch alone while the firmware failsafe exists."""
        failsafe = self.module.module / "ptconf" / ".failsafe"
        failsafe.parent.mkdir(parents=True)
        failsafe.touch()
        self.idle_line()
        log = self.watch(4)
        self.assertIn("Management port: firmware failsafe is present", log)
        check = self.module.invoke("--check")
        self.assertIn("the switch was not inspected", check.stdout)
        self.assertEqual([], self.lines("calls"))

    def test_fallback_waits_for_an_unchanged_window_without_a_stable_service(self) -> None:
        """Restore management when the service never settles, but only after two minutes."""
        self.configure(interval=60)
        self.service(pon_state=NOT_O5)
        log = self.watch(2)
        self.assertEqual([], self.writes())
        self.assertIn("waiting for a stable service or the 120s fallback", log)
        self.service(pon_state=NOT_O5)
        log = self.watch(3)
        self.assertEqual([LCT], self.writes())
        self.assertIn("(fallback: unchanged for at least 120s without a stable service)", log)
        self.assertEqual(HEALTHY, self.record())

    def test_changed_interface_inventory_restarts_the_fallback_window(self) -> None:
        """Treat recreated service interfaces as provisioning that is still moving."""
        self.configure(interval=60)
        self.service(pon_state=NOT_O5, events={"2": {"interfaces": ["gem9000"]}})
        self.watch(4)
        self.assertEqual([], self.writes())
        (self.module.net / "gem9000").unlink()
        self.service(pon_state=NOT_O5, events={"2": {"interfaces": ["gem9000"]}})
        self.watch(5)
        self.assertEqual([LCT], self.writes())

    def test_port_changing_before_the_write_is_left_alone(self) -> None:
        """Revalidate the sampled record immediately before writing."""
        for record in (HEALTHY, "0x1|0x4|0x1|0x18"):
            with self.subTest(record=record):
                self.service()
                self.ports({"3": UNI, LCT: EMPTY_MAP, "9": SERVICE})
                for reads in self.switch.glob("reads.*"):
                    reads.unlink()
                (self.switch / f"at.{LCT}.3").write_text(record + "\n")
                log = self.watch(2)
                self.assertEqual([], self.writes())
                self.assertEqual(record, self.record())
                self.assertIn("Management port changed during inspection", log)

    def test_unsuccessful_restores_are_bounded(self) -> None:
        """Stop retrying a write that fails or does not take effect."""
        for marker in ("ignore-writes", "fail-writes"):
            with self.subTest(marker=marker):
                self.idle_line()
                self.ports({"3": UNI, LCT: MOVED, "9": SERVICE})
                (self.switch / "writes").unlink(missing_ok=True)
                (self.switch / marker).touch()
                log = self.watch(9)
                (self.switch / marker).unlink()
                self.assertEqual([LCT, LCT, LCT], self.writes())
                self.assertEqual(MOVED, self.record())
                self.assertIn("readback is not the healthy state (attempt 3 of 3)", log)
                self.assertEqual(1, log.count("restore suspended after 3 unsuccessful attempts"))

    def test_port_detached_again_is_restored_again(self) -> None:
        """Follow later provisioning that detaches the port a second time."""
        self.service(events={"3": {"switch": {LCT: MOVED}}})
        log = self.watch(5)
        self.assertEqual([LCT, LCT], self.writes())
        self.assertEqual(HEALTHY, self.record())
        self.assertEqual(2, log.count("Management port restored:"))

    def test_clearing_the_opt_in_stops_recovery_before_a_write(self) -> None:
        """Honor a configuration change made while a sample is pending."""
        self.service(events={"1": {"config_text": self.configure(recovery=False)}})
        self.configure()
        self.watch(3)
        self.assertEqual([], self.writes())
        self.assertEqual(EMPTY_MAP, self.record())

    def test_missing_helper_is_reported_without_switch_access(self) -> None:
        """Report an incomplete installation instead of guessing."""
        self.helper.unlink()
        self.idle_line()
        log = self.watch(3)
        self.assertEqual(1, log.count("the recovery helper is missing or unreadable"))
        check = self.module.invoke("--check")
        self.assertIn("the recovery helper is missing or unreadable", check.stdout)
        self.assertEqual([], self.lines("calls"))

    def test_malformed_or_conflicting_replies_never_identify_a_port(self) -> None:
        """Reject incomplete, non-hexadecimal or self-contradicting switch output."""
        valid = "nBridgeId = 0x0\nnDestLogicalPortId = 0x4\nnDestSubIfIdGroup = 0x1\nnBridgePortMapIndex[0] = 0x0\n"
        replies = (
            valid.replace("nBridgePortMapIndex[0] = 0x0\n", ""),
            valid.replace("nBridgeId = 0x0", "nBridgeId = 0"),
            valid + "nBridgeId = 0x1\n",
            valid.replace("nDestSubIfIdGroup = 0x1", "nDestSubIfIdGroup"),
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.idle_line()
                (self.switch / f"raw.{LCT}").write_text(reply)
                log = self.watch(3)
                self.assertEqual([], self.writes())
                self.assertIn("no bridge port presents the supported LCT identity", log)

    def test_hexadecimal_formatting_variants_are_canonicalized(self) -> None:
        """Compare switch values numerically rather than by their spelling."""
        (self.switch / f"raw.{LCT}").write_text(
            "\t nBridgeId \t= 0x00\n\t nDestLogicalPortId \t= 0X04\n"
            "\t nDestSubIfIdGroup \t= 0x0001\n\t nBridgePortMapIndex[0] \t= 0x0000\n"
        )
        result = self.module.invoke("--check")
        self.module.assert_success(result)
        self.assertIn(
            "bridge-port=6 state=detached bridge=0x0 logical-port=0x4 sub-interface=0x1 map=0x0", result.stdout
        )

    def test_unresponsive_switch_is_bounded_without_writes(self) -> None:
        """Stop a stalled table read instead of waiting on every bridge port."""
        self.module.env["DEI835_SWITCH_TIMEOUT_SECONDS"] = "1"
        (self.switch / "hang").touch()
        started = time.monotonic()
        result = self.module.invoke("--check")
        elapsed = time.monotonic() - started
        self.module.assert_success(result)
        self.assertIn("the switch did not answer within the bounded interval", result.stdout)
        self.assertEqual(1, len(self.lines("calls")))
        self.assertLess(elapsed, 10)
        self.assertEqual([], self.writes())

    def test_invalid_switch_timeout_is_rejected_before_any_read(self) -> None:
        """Keep the switch timeout finite and within the documented bound."""
        for timeout in ("0", "31", "unbounded"):
            with self.subTest(timeout=timeout):
                self.idle_line()
                self.module.env["DEI835_SWITCH_TIMEOUT_SECONDS"] = timeout
                log = self.watch(3)
                self.assertIn("invalid switch timeout", log)
                self.assertEqual([], self.lines("calls"))

    def test_switch_utility_outside_the_module_root_is_refused(self) -> None:
        """Never bound or run a stand-in found outside the selected module."""
        outside = self.module.root / "host-bin"
        outside.mkdir()
        self.mock.rename(outside / "switch_cli")
        self.module.env["PATH"] = f"{self.module.env['PATH']}:{outside}"
        self.idle_line()
        log = self.watch(3)
        self.assertIn("cannot locate a bounded module switch_cli command", log)
        self.assertEqual([], self.lines("calls"))

    def test_helper_refuses_to_load_without_the_shared_library(self) -> None:
        """Fail closed when the optional helper is sourced by itself."""
        result = subprocess.run(
            [BUSYBOX, "ash", "-c", '. "$1"; echo loaded', "offline-helper", str(self.helper)],
            env=self.module.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("loaded", result.stdout)
        self.assertEqual([], self.lines("calls"))


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class ManagementRecoveryInstallerTests(unittest.TestCase):
    """Verify installer preflight and packaging of the optional helper."""

    def setUp(self) -> None:
        """Select the recovery opt-in for an otherwise working synthetic module."""
        self.helper = installer_support.InstallerTests("test_check_does_not_install_or_modify_configuration")
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.fixture = self.helper.fixture
        self.conf = self.helper.conf
        self.switch = self.fixture.root / "switch"
        self.switch.mkdir()
        self.helper.env["DEI835_MOCK_SWITCH"] = str(self.switch)
        self.helper.config.write_text("schema_version = 1\n[management_recovery]\nenabled = true\n")

    def provide_switch(self, record: str) -> None:
        """Add the switch stand-in and one management port at bridge port 5."""
        mock = self.fixture.bin / "switch_cli"
        mock.write_text(SWITCH_MOCK)
        mock.chmod(0o755)
        for interface in ("eth0_0", "eth0_0_1_lct"):
            (self.fixture.net / interface).touch()
        (self.switch / "port.5").write_text(record + "\n")

    def test_opt_in_without_the_switch_utility_is_refused_before_any_change(self) -> None:
        """Stop before auditing, saving or installing when recovery could never run."""
        for action in ("check", "install"):
            with self.subTest(action=action):
                result = self.helper.invoke(action)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("Management recovery is enabled, but a required command is missing", result.stderr)
                self.helper.assert_runtime_absent()
                self.assertFalse((self.conf / "dei-pon.toml").exists())
                self.assertFalse((self.conf / "install.log").exists())
                self.assertFalse((self.conf / "setup-state").exists())

    def test_default_configuration_installs_the_helper_without_using_the_switch(self) -> None:
        """Ship the sixth runtime file while leaving it unused by default."""
        self.helper.config.write_text("schema_version = 1\n")
        self.provide_switch(EMPTY_MAP)
        result = self.helper.invoke("install", allow_fw=True)
        self.helper.success(result)
        self.assertIn("Management recovery enabled: false", result.stdout)
        self.assertIn("Management recovery: disabled", result.stdout)
        self.assertNotIn("may later restore", result.stdout)
        self.assertEqual((SOURCE / "dei-lct.sh").read_bytes(), (self.conf / "dei-lct.sh").read_bytes())
        self.assertFalse((self.switch / "calls").exists())
        self.assertIn("management-recovery=false", (self.conf / "install.log").read_text())

    def test_opt_in_is_reported_read_only_and_installed_without_a_write(self) -> None:
        """Plan and install the opt-in without the installer touching a bridge port."""
        self.provide_switch(MOVED)
        result = self.helper.invoke("check")
        self.helper.success(result)
        self.assertIn("Management recovery enabled: true", result.stdout)
        self.assertIn("Management port: interface=eth0_0_1_lct bridge-port=5 state=detached", result.stdout)
        self.helper.assert_runtime_absent()
        result = self.helper.invoke("install", allow_fw=True)
        self.helper.success(result)
        self.assertIn("the watcher may later restore the verified LCT bridge port", result.stdout)
        for name in installer_support.RUNTIME:
            self.assertEqual((SOURCE / name).read_bytes(), (self.conf / name).read_bytes())
        audit = (self.conf / "install.log").read_text()
        self.assertIn("management-recovery=true", audit)
        self.assertIn("bridge-port=5 state=detached", audit)
        self.assertIn("enabled = true", (self.conf / "dei-pon.toml").read_text())
        self.assertFalse((self.switch / "writes").exists())
        self.assertFalse((self.switch / "errors").exists())
        self.assertEqual(MOVED, (self.switch / "port.5").read_text().strip())
        self.helper.success(self.helper.invoke("rollback"))
        self.helper.assert_runtime_absent()


if __name__ == "__main__":
    unittest.main()
