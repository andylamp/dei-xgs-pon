"""Check GEM counter and encryption observations through the offline installer."""

import json
import shutil
import unittest

import test_module_diagnostics as support
from paths import BUSYBOX

PONTOP_MOCK = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

if sys.argv[1:] != ["-b", "-g", "GEM/XGEM Port Counters"]:
    raise SystemExit(2)
source = Path(os.environ["DEI_TEST_GEM_TABLES"])
counter = source.with_suffix(".reads")
index = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(index + 1))
table = json.loads(source.read_text())[index]
print(table["output"])
raise SystemExit(table["exit"])
"""

HEADER = (
    "Page: GEM/XGEM Port Counters\n"
    "GEM Index       GEM ID          u/s packets     u/s bytes       d/s packets     d/s bytes       Key Errors\n"
)

# Modeled on the WAS-110 pontop page. 1081 is outside VLAN 835's mapper.
FIRST = (
    HEADER
    + """0 5    598 28704 598   28704   0
1 1084 552 139724 491   48805   491
5 1085 18  2028   28848 2712030 28825
6 1081 73  16618  1734  162996  1734
9 1086 16  1760   0     0       0
13 1087 16 1760   0     0       0"""
)

LAST = (
    HEADER
    + """0 5    598 28704 598   28704   0
1 1084 595 152002 507   50407   500
5 1085 18  2028   28981 2724532 28825
6 1081 73  16618  1744  163936  1744
9 1086 16  1760   0     0       0
13 1087 16 1760   12    1134    0"""
)


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class GemDiagnosticsTests(unittest.TestCase):
    """Distinguish current, historical, and unreliable evidence without live changes."""

    def setUp(self) -> None:
        """Reuse the isolated installer and provide hardware-shaped command replies."""
        self.module = support.ModuleDiagnosticsTests("test_state_transition_is_reported")
        self.module.setUp()
        self.addCleanup(self.module.tearDown)
        self.helper = self.module.helper
        self.module.configure_pon([self.module.sample(300), self.module.sample(302)])
        self.tables = self.helper.fixture.root / "gem-tables.json"
        self.helper.env["DEI_TEST_GEM_TABLES"] = str(self.tables)
        self.pontop = self.helper.module / "usr/bin/pontop"
        self.pontop.write_text(PONTOP_MOCK)
        self.pontop.chmod(0o755)

    def diagnose(
        self,
        last: str = LAST,
        *,
        first: str = FIRST,
        final_status: int = 0,
        samples: int = 2,
        config_args: tuple[str, ...] = (),
    ) -> str:
        """Collect a synthetic window and check that the device remains unchanged.

        Returns:
            The complete standard output of the installer.

        """
        self.tables.write_text(json.dumps([{"output": first, "exit": 0}, {"output": last, "exit": final_status}]))
        self.tables.with_suffix(".reads").unlink(missing_ok=True)
        self.helper.fixture.state["calls"] = []
        result = self.helper.invoke("diagnose", extra=("--samples", str(samples), "--interval", "2", *config_args))
        self.helper.success(result)
        self.module.assert_read_only()
        return result.stdout

    def omci_reads(self) -> list[list[str]]:
        """Get actual OMCI calls recorded by the mock device.

        Returns:
            Vendor arguments in execution order.

        """
        return [call[1:] for call in self.helper.fixture.state["calls"] if call[0] == "omci_pipe.sh"]

    def test_rising_errors_read_only_the_selected_port_and_compare_the_key_ring(self) -> None:
        """Inspect the affected GEM while distinguishing quiet ports and old errors."""
        self.helper.fixture.state["gems"]["1084"]["enc"] = 1
        self.helper.fixture.state["omci_readback_replies"] = {"1084": "errorcode=0 attr_data=00\n"}
        report = self.diagnose()
        self.assertIn("GEM 1084: downstream-delta=16 key-error-delta=9", report)
        self.assertIn("OMCI key-ring=0 driver=1 target=3", report)
        self.assertIn("GEM 1085: downstream-delta=133 key-error-delta=0 (historical key errors", report)
        self.assertIn("GEM 1086: downstream-delta=0 key-error-delta=0 (no downstream traffic", report)
        self.assertIn("GEM 1087: downstream-delta=12 key-error-delta=0 (no key errors", report)
        self.assertNotIn("GEM 1081: downstream-delta=", report)
        self.assertEqual([["meadg", "268", "1084", "1"], ["meadg", "268", "1084", "10"]], self.omci_reads())
        self.assertEqual("2", self.tables.with_suffix(".reads").read_text())

    def test_mismatched_omci_port_id_blocks_attribute_ten(self) -> None:
        """Keep OMCI reads on the selected GEM when instance IDs differ from Port-IDs."""
        self.helper.fixture.state["omci_port_ids"] = {"1084": 1081}
        report = self.diagnose()
        self.assertIn("GEM 1084: downstream-delta=16 key-error-delta=9", report)
        self.assertIn("GEM 1084: OMCI key-ring unavailable", report)
        self.assertNotIn("OMCI key-ring=", report)
        self.assertEqual([["meadg", "268", "1084", "1"]], self.omci_reads())

    def test_vlan_836_keeps_its_selected_gem_ids_and_configured_key_ring(self) -> None:
        """Compare another service against its own target when errors increase."""
        selected = self.helper.fixture.root / "selected.toml"
        selected.write_text("schema_version=1\n[internet]\nvlan=836\nencryption_key_ring=1\n")
        self.helper.fixture.state["gems"]["1081"]["enc"] = 1
        other_members = "\n2 1080 0 0 1 94 0\n10 1082 0 0 0 0 0\n14 1083 0 0 0 0 0"
        report = self.diagnose(
            LAST + other_members, first=FIRST + other_members, config_args=("--config", str(selected))
        )
        self.assertIn("GEM 1081: downstream-delta=10 key-error-delta=10", report)
        self.assertIn("OMCI key-ring=1 driver=1 target=1", report)
        self.assertNotIn("GEM 1084: downstream-delta=", report)
        self.assertEqual([["meadg", "268", "1081", "1"], ["meadg", "268", "1081", "10"]], self.omci_reads())

    def test_malformed_attribute_reply_remains_unknown_and_private(self) -> None:
        """Keep malformed ring values out of the report and out of decisions."""
        self.helper.fixture.state["omci_readback_replies"] = {"1084": "errorcode=0 attr_data=03PRIVATE"}
        report = self.diagnose()
        self.assertNotIn("03PRIVATE", report)
        self.assertNotIn("OMCI key-ring=3", report)
        self.assertEqual([["meadg", "268", "1084", "1"], ["meadg", "268", "1084", "10"]], self.omci_reads())

    def test_reset_missing_and_duplicate_rows_cannot_trigger_omci(self) -> None:
        """Require unique, complete, nondecreasing rows from two successful samples."""
        active = next(line for line in LAST.splitlines() if line.startswith("1 1084 "))
        for changed in (
            LAST.replace("507   50407   500", "10    500     6"),
            LAST.replace(active + "\n", ""),
            LAST + "\n" + active,
            LAST.replace("507   50407   500", "broken 50407   500"),
            LAST.replace("1 1084", "17 1084"),
        ):
            with self.subTest(changed=changed):
                report = self.diagnose(changed)
                self.assertNotIn("GEM 1084: downstream-delta=", report)
                self.assertEqual([], self.omci_reads())

    def test_counter_command_failure_discards_plausible_output(self) -> None:
        """Preserve PON observations if a vendor command prints numbers then fails."""
        report = self.diagnose(final_status=1)
        self.assertIn("PON summary:", report)
        self.assertNotIn("GEM 1084: downstream-delta=", report)
        self.assertEqual([], self.omci_reads())

    def test_large_counters_preserve_single_packet_deltas(self) -> None:
        """Compare 64-bit counters exactly with the BusyBox applet and the host awk."""
        first = FIRST.replace("491   48805   491", "9007199254740992 48805 9007199254740992")
        last = LAST.replace("507   50407   500", "9007199254740993 50407 9007199254740993")
        launcher = self.helper.fixture.bin / "launch-ash"
        original = launcher.read_text()
        # Standalone ash can prefer applets over PATH. Select each awk explicitly.
        commands = {"busybox": '"$DEI_TEST_BUSYBOX" awk'}
        if host_awk := shutil.which("awk"):
            self.helper.env["DEI_TEST_HOST_AWK"] = host_awk
            commands["host"] = '"$DEI_TEST_HOST_AWK"'
        for label, command in commands.items():
            with self.subTest(awk=label):
                launcher.write_text(f'awk() {{ {command} "$@"; }}\n' + original)
                self.module.configure_pon([self.module.sample(300), self.module.sample(302)])
                report = self.diagnose(last, first=first)
                self.assertIn("GEM 1084: downstream-delta=1 key-error-delta=1 (rising key errors)", report)
                self.assertEqual([["meadg", "268", "1084", "1"], ["meadg", "268", "1084", "10"]], self.omci_reads())

    def test_changed_mapper_membership_blocks_comparison(self) -> None:
        """Reject repeated Port-IDs if the selected service changes between snapshots."""
        mapper = self.helper.fixture.state["mappers"]["pmapper1"].copy()
        mapper["gems"] = [1084, 1085]
        self.helper.fixture.state["events"] = {"2": {"mappers": {"pmapper1": mapper}}}
        report = self.diagnose()
        self.assertNotIn("GEM 1084: downstream-delta=", report)
        self.assertEqual([], self.omci_reads())

    def test_missing_optional_counter_tool_preserves_pon_report(self) -> None:
        """Report available PON state even on firmware lacking pontop."""
        self.pontop.unlink()
        report = self.diagnose()
        self.assertIn("PON summary:", report)
        self.assertNotIn("GEM 1084: downstream-delta=", report)
        self.assertEqual([], self.omci_reads())

    def test_reassignment_during_port_id_read_stops_further_omci_inspection(self) -> None:
        """An OLT can change mapper ownership while a read-only vendor call is in flight."""
        mapper = self.helper.fixture.state["mappers"]["pmapper1"].copy()
        mapper["gems"] = [1085, 1086, 1087]
        self.helper.fixture.state["omci_port_events"] = {"1084": {"1": {"mappers": {"pmapper1": mapper}}}}
        report = self.diagnose()
        self.assertIn("GEM 1084: OMCI key-ring unavailable", report)
        self.assertEqual([["meadg", "268", "1084", "1"]], self.omci_reads())

    def test_single_sample_has_no_counter_delta(self) -> None:
        """An instantaneous cumulative total cannot identify a current fault."""
        report = self.diagnose(samples=1)
        self.assertIn("PON summary:", report)
        self.assertFalse(self.tables.with_suffix(".reads").exists())
        self.assertNotIn("GEM 1084: downstream-delta=", report)
        self.assertEqual([], self.omci_reads())

    def test_pon_age_reset_invalidates_counter_comparison(self) -> None:
        """Reject deltas across different activation sessions even if port IDs match."""
        self.module.configure_pon([self.module.sample(300), self.module.sample(2)])
        report = self.diagnose()
        self.assertIn("timer-resets=1", report)
        self.assertNotIn("GEM 1084: downstream-delta=", report)
        self.assertEqual([], self.omci_reads())
