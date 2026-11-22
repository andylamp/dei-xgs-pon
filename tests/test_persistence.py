"""Offline contract tests for the DEI repair scripts; no device access.

Run from the checkout: python3 -m unittest discover -s tests -v
The shell scripts execute under BusyBox ash with all device-facing commands
replaced by JSON-backed fakes. Fixtures preserve the relevant formatting from
the user's tc and ip dumps, including doubled spaces and native OMCI rules.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from paths import BUSYBOX, SOURCE

BAD_RULE = """filter protocol 802.1Q pref 1 flower
filter protocol 802.1Q pref 1 flower handle 0x1
  vlan_id 835
  skip_sw
        action order 1: vlan  modify id 836 protocol 802.1Q pass
         index 130 ref 1 bind 1

filter protocol 802.1Q pref 2 flower
filter protocol 802.1Q pref 2 flower handle 0x2
  skip_sw
        action order 1: gact action drop
         random type none pass val 0
         index 229 ref 1 bind 1

filter protocol all pref 3 flower
filter protocol all pref 3 flower handle 0x3
  skip_sw
        action order 1: gact action drop
         random type none pass val 0
         index 230 ref 1 bind 1
"""
FIXED_RULE = BAD_RULE.replace("modify id 836", "modify id 835")
NATIVE_RULE = """filter protocol 802.1Q pref 1 flower
filter protocol 802.1Q pref 1 flower handle 0x2
  indev pmapper1
  vlan_id 835
  skip_sw
        action order 1: gact action pass
         random type none pass val 0
         index 209 ref 1 bind 1

filter protocol 802.1Q pref 65001 flower
filter protocol 802.1Q pref 65001 flower handle 0x4
  indev pmapper1
  skip_sw
        action order 1: gact action drop
         random type none pass val 0
         index 213 ref 1 bind 1
"""

LEGACY_836_INGRESS = """filter protocol 802.1Q pref 1 flower
filter protocol 802.1Q pref 1 flower handle 0x1
  skip_sw
        action order 1: vlan  pop pass
         index 127 ref 1 bind 1
"""
LEGACY_836_EGRESS = """filter protocol 802.1Q pref 1 flower
filter protocol 802.1Q pref 1 flower handle 0x1
  skip_sw
        action order 1: gact action drop
         random type none pass val 0
         index 228 ref 1 bind 1

filter protocol all pref 2 flower
filter protocol all pref 2 flower handle 0x2
  skip_sw
        action order 1: vlan  push id 836 protocol 802.1Q priority 0 pass
         index 129 ref 1 bind 1
"""

MOCK_PROGRAM = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import sys

state_path = Path(os.environ["DEI835_MOCK_STATE"])
state = json.loads(state_path.read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state.setdefault("calls", []).append([name, *args])
code = 0
output = ""

def mutation(kind, **data):
    state.setdefault("mutations", []).append({"kind": kind, **data})

try:
    if name == "ip":
        assert args[:5] == ["-d", "link", "show", "dev", args[4]], args
        dev = args[4]
        if dev in state["mappers"]:
            mapper = state["mappers"][dev]
            output = "%s: %s@pon0: <UP,LOWER_UP> mtu 1500 master sw257\n" % (mapper.get("ifindex", 46), dev)
            output += "    pmapper mode: pcp "
            for pcp, gem in enumerate(mapper["gems"]):
                output += "pcp %d: gem%s ctp: %d " % (pcp, gem, 11 + pcp)
            # Real dump repeats some GEMs for multiple PCPs; dedupe is required.
            output += "pcp 6: gem%s pcp 7: gem%s pcpdef: 0 bp: 27\n" % (
                mapper["gems"][0], mapper["gems"][-1])
        elif dev.startswith("gem") and dev[3:] in state["gems"]:
            gem_id = dev[3:]
            gem = state["gems"][gem_id]
            output = "50: %s@pon0: <UP,LOWER_UP> mtu 1500\n" % dev
            output += ("    gem idx: 1 id: %s traffic type: %s dir: %s enc: %s "
                       "max size: 4095 tcont: tcont32768 mc: %s bp: 27 ctp: 11\n") % (
                gem_id, gem.get("type", 0), gem.get("dir", 3), gem["enc"],
                gem.get("mc", 0))
        else:
            code = 1
    elif name == "tc":
        assert args[:2] in (["filter", "show"], ["filter", "replace"]), args
        dev = args[args.index("dev") + 1]
        direction = "ingress" if "ingress" in args else "egress"
        assert direction in args, args
        key = "ingress" if direction == "ingress" else "rules"
        if args[1] == "show":
            output = state["mappers"][dev].get(key, "")
        else:
            base = ["filter", "replace", "dev", dev, direction, "protocol"]
            if direction == "egress" and "vlan_id" in args:
                vid = args[args.index("vlan_id") + 1]
                assert vid in ("835", "836"), args
                expected = base + ["802.1Q", "pref", "1", "handle", "0x1", "flower", "skip_sw",
                                   "vlan_id", vid, "action", "vlan", "modify", "id", vid,
                                   "protocol", "802.1Q", "pass"]
                step = vid + "-allow" if vid == "836" else "835-repair"
            elif direction == "ingress":
                expected = base + ["802.1Q", "pref", "1", "handle", "0x1", "flower", "skip_sw",
                                   "action", "vlan", "modify", "id", "836", "protocol", "802.1Q", "pass"]
                step = "836-keep"
            else:
                expected = base + ["all", "pref", "2", "handle", "0x2", "flower", "skip_sw", "action", "drop"]
                step = "836-drop"
            assert args == expected, (args, expected)
            mutation("tc_replace", dev=dev, direction=direction, step=step, args=args)
            if state.get("tc_failure") or state.get("tc_fail_on") == step:
                output = "RTNETLINK answers: Operation not supported\n"
                code = 2
            elif not state.get("tc_ignore_write"):
                old = state["mappers"][dev][key]
                if step == "835-repair":
                    assert "vlan  modify id 836 protocol 802.1Q pass" in old, old
                    new = old.replace("vlan  modify id 836 protocol 802.1Q pass", "vlan  modify id 835 protocol 802.1Q pass", 1)
                elif step == "836-allow":
                    marker = "filter protocol 802.1Q pref 1 flower handle 0x1\n"
                    assert marker in old, old
                    before, rest = old.split(marker, 1)
                    block, following = rest.split("filter protocol all pref 2", 1)
                    assert "gact action drop" in block and "vlan_id" not in block, block
                    block = "  vlan_id 836\n" + block.replace("gact action drop", "vlan  modify id 836 protocol 802.1Q pass", 1)
                    new = before + marker + block + "filter protocol all pref 2" + following
                elif step == "836-keep":
                    assert "vlan  pop pass" in old, old
                    new = old.replace("vlan  pop pass", "vlan  modify id 836 protocol 802.1Q pass", 1)
                else:
                    assert "vlan  push id 836 protocol 802.1Q priority 0 pass" in old, old
                    new = old.replace("vlan  push id 836 protocol 802.1Q priority 0 pass", "gact action drop", 1)
                state["mappers"][dev][key] = new
                for changed_dev, mapper in state.get("tc_mapper_events", {}).get(step, {}).items():
                    state["mappers"][changed_dev] = mapper
    elif name == "omci_pipe.sh":
        assert len(args) in (4, 5), args
        operation, me, gem_id, attribute = args[:4]
        assert me == "268" and attribute in ("1", "10"), args
        assert gem_id in state["gems"], args
        port_id = str(state.get("omci_port_ids", {}).get(gem_id, int(gem_id)))
        if attribute == "1":
            assert operation == "meadg" and len(args) == 4, args
            reads = state.setdefault("omci_port_reads", {})
            index = reads.get(gem_id, 0)
            reads[gem_id] = index + 1
            event = state.get("omci_port_events", {}).get(gem_id, {}).get(str(index + 1), {})
            for changed_dev, mapper in event.get("mappers", {}).items():
                state["mappers"][changed_dev] = mapper
            if "replace_gems" in event:
                state["gems"] = event["replace_gems"]
            if "config_text" in event:
                (Path(os.environ["DEI835_CONF_DIR"]) / "dei-pon.toml").write_text(event["config_text"])
            ports = state.get("omci_port_id_sequences", {}).get(gem_id, [])
            if ports:
                port_id = str(ports[min(index, len(ports) - 1)])
                state.setdefault("omci_port_ids", {})[gem_id] = int(port_id)
            output = state.get("omci_port_replies", {}).get(
                gem_id, "errorcode=0 attr_data=%02X %02X\n" % (int(port_id) >> 8, int(port_id) & 255))
            if gem_id in state.get("omci_port_failures", []):
                code = 1
        elif operation == "meads":
            assert len(args) == 5 and args[4] in ("1", "3"), args
            value = int(args[4])
            mutation("omci_set", gem=gem_id, port=port_id, value=value)
            if gem_id in state.get("omci_failures", []):
                # Firmware wrapper returns shell success even on OMCI error.
                output = "errorcode=-16\n"
            else:
                if gem_id not in state.get("omci_ignore_writes", []):
                    state["gems"][port_id]["enc"] = value
                output = "errorcode=0\n"
            output = state.get("omci_set_replies", {}).get(gem_id, output)
        else:
            assert operation == "meadg" and len(args) == 4, args
            output = "errorcode=0 attr_data=%02x\n" % state["gems"][port_id]["enc"]
            output = state.get("omci_readback_replies", {}).get(gem_id, output)
    elif name == "pon":
        if args == ["psg"]:
            output = state.get("pon_reply",
                "errorcode=0 current=%s previous=40 time_curr=3305\n" % state.get("pon_state", 51))
            code = state.get("pon_exit_code", 0)
        else:
            assert len(args) == 2 and args[0] == "gpcg", args
            assert args[1] in state["gems"], args
            output = "errorcode=0 gem_port_id=%s rx_frames=100 key_errors=50\n" % args[1]
            output = state.get("pon_counter_replies", {}).get(args[1], output)
    elif name == "fw_printenv":
        assert len(args) == 2 and args[0] == "-n", args
        if args[1] == "8311_fix_vlans":
            output = str(state.get("fix_mode", 2)) + "\n"
            code = state.get("fix_mode_exit_code", 0)
        else:
            assert args[1] in ("mib_file", "8311_persist_root", "bootcmd", "8311_mib_file", "8311_gpon_sn", "8311_omcc_version", "8311_vendor_id", "8311_equipment_id", "8311_hw_ver", "8311_failsafe_delay", "8311_ipaddr", "8311_netmask", "8311_gateway", "8311_ping_ip", "8311_cp_hw_ver_sync"), args
            if args[1] in state.get("fwenv", {}):
                output = str(state["fwenv"][args[1]]) + "\n"
            else:
                print(f'## Error: "{args[1]}" not defined', file=sys.stderr)
                code = 1
    elif name == "fw_setenv":
        assert len(args) in (1, 2), args
        value = args[1] if len(args) == 2 else None
        mutation("fwenv_set", key=args[0], value=value)
        if value is None:
            state.setdefault("fwenv", {}).pop(args[0], None)
        else:
            state.setdefault("fwenv", {})[args[0]] = value
        if args[0] == "8311_fix_vlans":
            state["fix_mode"] = "" if value is None else int(value)
            state["fix_mode_exit_code"] = int(value is None)
    elif name == "sleep":
        state["sleeps"] = state.get("sleeps", 0) + 1
        event = state.get("events", {}).get(str(state["sleeps"]), {})
        for gem_id, value in event.get("encryption", {}).items():
            state["gems"][gem_id]["enc"] = value
        for dev, mapper in event.get("mappers", {}).items():
            state["mappers"][dev] = mapper
        if "replace_gems" in event:
            state["gems"] = event["replace_gems"]
        if "fix_mode" in event:
            state["fix_mode"] = event["fix_mode"]
        if "pon_state" in event:
            state["pon_state"] = event["pon_state"]
        if "config_text" in event:
            (Path(os.environ["DEI835_CONF_DIR"]) / "dei-pon.toml").write_text(event["config_text"])
        if event.get("failsafe"):
            failsafe = Path(os.environ["DEI835_MODULE_ROOT"]) / "ptconf" / ".failsafe"
            failsafe.parent.mkdir(parents=True, exist_ok=True)
            failsafe.touch()
        if event.get("stop"):
            (Path(os.environ["DEI835_RUN_DIR"]) / "stop").touch()
    else:
        raise AssertionError("Unexpected fake device command: " + name)
except Exception as error:
    state.setdefault("mock_errors", []).append(repr(error))
    output = "MOCK ERROR: %r\n" % error
    code = 97

state_path.write_text(json.dumps(state))
sys.stdout.write(output)
sys.exit(code)
"""


# The fake driver accepts integer-valued attributes such as enc, mc and dir.
type GemMockState = dict[str, int]


class MapperMockState(TypedDict):
    """Describe one service mapper and its simulated traffic-control rules."""

    gems: list[int]
    rules: str
    ingress: NotRequired[str]
    ifindex: NotRequired[int]


class OmciMutation(TypedDict):
    """Record a simulated encryption-attribute write."""

    kind: Literal["omci_set"]
    gem: str
    port: str
    value: int


class TcMutation(TypedDict):
    """Record a simulated replacement of one traffic-control rule."""

    kind: Literal["tc_replace"]
    dev: str
    direction: str
    step: str
    args: list[str]


class FirmwareMutation(TypedDict):
    """Record a simulated firmware-environment update."""

    kind: Literal["fwenv_set"]
    key: str
    value: str | None


type MockMutation = OmciMutation | TcMutation | FirmwareMutation


class ModuleEvent(TypedDict, total=False):
    """Describe module or configuration changes injected between polling passes."""

    encryption: dict[str, int]
    mappers: dict[str, MapperMockState]
    replace_gems: dict[str, GemMockState]
    fix_mode: int | str
    pon_state: int
    config_text: str
    failsafe: bool
    stop: bool


class ModuleMockState(TypedDict):
    """Describe isolated device state and the command history recorded by fakes."""

    mappers: dict[str, MapperMockState]
    gems: dict[str, GemMockState]
    calls: list[list[str]]
    mutations: list[MockMutation]
    sleeps: int
    fwenv: NotRequired[dict[str, str]]
    events: NotRequired[dict[str, ModuleEvent]]
    fix_mode: NotRequired[int | str]
    fix_mode_exit_code: NotRequired[int]
    pon_state: NotRequired[int]
    pon_reply: NotRequired[str]
    pon_exit_code: NotRequired[int]
    pon_counter_replies: NotRequired[dict[str, str]]
    tc_failure: NotRequired[bool]
    tc_ignore_write: NotRequired[bool]
    tc_fail_on: NotRequired[str]
    tc_mapper_events: NotRequired[dict[str, dict[str, MapperMockState]]]
    omci_failures: NotRequired[list[str]]
    omci_ignore_writes: NotRequired[list[str]]
    omci_set_replies: NotRequired[dict[str, str]]
    omci_readback_replies: NotRequired[dict[str, str]]
    omci_port_ids: NotRequired[dict[str, int]]
    omci_port_reads: NotRequired[dict[str, int]]
    omci_port_replies: NotRequired[dict[str, str]]
    omci_port_id_sequences: NotRequired[dict[str, list[int]]]
    omci_port_failures: NotRequired[list[str]]
    omci_port_events: NotRequired[dict[str, dict[str, ModuleEvent]]]
    mock_errors: NotRequired[list[str]]


INITIAL_CORRECTED_GEM = 1085
DOWNSTREAM_ENCRYPTION = 3


def initial_state(rule: str = BAD_RULE) -> ModuleMockState:
    """Create the default three-service topology used by runtime tests.

    Args:
        rule: Egress rules to install on the fake VLAN-835 mapper.

    Returns:
        Isolated mapper, GEM, and command-history state.

    """
    return {
        "mappers": {
            "pmapper1": {"gems": [1084, 1085, 1086, 1087], "rules": rule},
            "pmapper2": {
                "gems": [1080, 1081, 1082, 1083],
                "rules": NATIVE_RULE.replace("pmapper1", "pmapper2").replace("835", "836"),
            },
            "pmapper3": {
                "gems": [1072, 1073, 1074, 1075],
                "rules": NATIVE_RULE.replace("pmapper1", "pmapper3").replace("835", "837"),
            },
        },
        "gems": {
            str(gem): {"enc": DOWNSTREAM_ENCRYPTION if gem == INITIAL_CORRECTED_GEM else 0, "mc": 0, "dir": 3}
            for gem in range(1072, 1088)
        },
        "calls": [],
        "mutations": [],
        "sleeps": 0,
    }


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class PersistenceTests(unittest.TestCase):
    """Exercise service discovery, repair, and watcher recovery using mocked devices."""

    def setUp(self) -> None:
        """Create isolated module files and replace device commands with controlled fakes."""
        self.temporary = tempfile.TemporaryDirectory(prefix="dei835-test-")
        self.root = Path(self.temporary.name)
        self.conf = self.root / "conf"
        self.run_dir = self.root / "run"
        self.net = self.root / "net"
        self.module = self.root / "module"
        self.bin = self.module / "bin"
        for path in (self.conf, self.net, self.bin):
            path.mkdir(parents=True)
        for name in ("dei-835-lib.sh", "keyring.sh", "status.sh", "dei-config.sh"):
            shutil.copyfile(SOURCE / name, self.conf / name)
        program = self.bin / "mock_device.py"
        program.write_text(MOCK_PROGRAM)
        program.chmod(0o755)
        for name in ("ip", "tc", "omci_pipe.sh", "pon", "sleep", "fw_printenv"):
            if name == "pon":
                # Scope-checked timeout callers execute a real module-root file.
                shutil.copyfile(program, self.bin / name)
                (self.bin / name).chmod(0o755)
            else:
                (self.bin / name).symlink_to(program)
        self.state_path = self.root / "state.json"
        self.state = initial_state()
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "DEI835_CONF_DIR": str(self.conf),
            "DEI835_RUN_DIR": str(self.run_dir),
            "DEI835_NET_DIR": str(self.net),
            "DEI835_MODULE_ROOT": str(self.module),
            "DEI835_MOCK_STATE": str(self.state_path),
            "DEI835_MOCK_BIN": str(self.bin),
            "DEI835_SETTLE_SECONDS": "0",
            "DEI835_POLL_SECONDS": "0",
        }
        for selector in ("DEI_PON_CONFIG_PATH", "DEI835_CONFIG_PATH", "DEI_PON_CONF_DIR", "DEI_PON_RUN_DIR"):
            self.env.pop(selector, None)

    def tearDown(self) -> None:
        """Remove the temporary filesystem created for this test."""
        self.temporary.cleanup()

    def invoke(
        self, mode: str = "--once", *, script: str = "keyring.sh", passes: int = 0
    ) -> subprocess.CompletedProcess[str]:
        """Run a runtime command against isolated module state.

        Args:
            mode: Runtime subcommand, or an empty string for the status script.
            script: Installed shell script to exercise.
            passes: Optional polling-pass limit for a simulated watcher.

        Returns:
            Captured process result after refreshing the recorded mock state.

        """
        self.state_path.write_text(json.dumps(self.state))
        for dev in self.state["mappers"]:
            (self.net / dev).touch()
        for gem in self.state["gems"]:
            (self.net / ("gem" + gem)).touch()
        (self.net / "gem-omci").touch()
        (self.net / "gem2046").touch()
        # This host's BusyBox ash prefers standalone applets over PATH for
        # ip/tc/sleep. Functions must intercept those commands too. Source the
        # actual script after installing proxies, retaining target shell syntax.
        proxies = "\n".join(
            f'{name}() {{ "$DEI835_MOCK_BIN/{name}" "$@"; }}' for name in ("ip", "tc", "pon", "sleep", "fw_printenv")
        )
        launcher = proxies + '\nscript=$1; shift; . "$script"\n'
        result = subprocess.run(
            [BUSYBOX, "ash", "-c", launcher, "offline-test", str(self.conf / script), *([mode] if mode else [])],
            env={**self.env, "DEI835_MAX_PASSES": str(passes)},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.state = cast("ModuleMockState", json.loads(self.state_path.read_text()))
        self.assertEqual([], self.state.get("mock_errors", []), result.stdout + result.stderr)
        return result

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        """Require a captured command to finish successfully.

        Args:
            result: Captured shell process whose output explains any failure.

        """
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def applied_gems(self) -> list[str]:
        """Collect the GEM IDs targeted by recorded encryption writes.

        Returns:
            GEM IDs in the order of their simulated OMCI writes.

        """
        return [mutation["gem"] for mutation in self.state["mutations"] if mutation["kind"] == "omci_set"]

    def configure(
        self, *, vlan: int = 835, encryption: int = 3, normalize: bool = True, enabled: bool = True, interval: int = 10
    ) -> str:
        """Write runtime settings into the fixture's TOML configuration.

        Args:
            vlan: Internet service to select.
            encryption: Desired unicast encryption key-ring value.
            normalize: Whether recognized VLAN repairs are permitted.
            enabled: Whether continuous watching is enabled.
            interval: Polling interval to record in seconds.

        Returns:
            TOML text that can also be injected during a polling pass.

        """
        text = (
            f"schema_version = 1\n[internet]\nvlan = {vlan}\n"
            f"encryption_key_ring = {encryption}\nnormalize_vlan = {str(normalize).lower()}\n"
            f"[watcher]\nenabled = {str(enabled).lower()}\ninterval_seconds = {interval}\n"
        )
        (self.conf / "dei-pon.toml").write_text(text)
        return text

    def legacy_836(self) -> None:
        """Replace fake VLAN-836 rules with the observed legacy untagged profile."""
        self.state["mappers"]["pmapper2"]["rules"] = LEGACY_836_EGRESS
        self.state["mappers"]["pmapper2"]["ingress"] = LEGACY_836_INGRESS

    def test_check_reports_actual_gems_and_makes_no_changes(self) -> None:
        """Verify that check reports actual GEMs and makes no changes."""
        before = copy.deepcopy(self.state)
        result = self.invoke("--check")
        self.assert_success(result)
        self.assertIn("GEMs=1084 1085 1086 1087", result.stdout)
        self.assertIn("rules=repair", result.stdout)
        self.assertIn("current mapper membership; no fixed GEM range", result.stdout)
        self.assertIn("Observed numeric GEM interfaces (all services; not an apply target):", result.stdout)
        self.assertIn(
            "1072 1073 1074 1075 1076 1077 1078 1079 1080 1081 1082 1083 1084 1085 1086 1087 2046", result.stdout
        )
        for gem in (1084, 1085, 1086, 1087):
            self.assertIn(f"GEM {gem}: role=unicast-data driver-id={gem}", result.stdout)
        self.assertIn("traffic-type=0 direction=3 multicast=0", result.stdout)
        self.assertIn("encryption=0 target-encryption=3", result.stdout)
        self.assertEqual([], self.state["mutations"])
        self.assertEqual(before["gems"], self.state["gems"])
        self.assertEqual(before["mappers"], self.state["mappers"])
        self.assertFalse(self.run_dir.exists())
        self.assertFalse((self.conf / "dei835.log").exists())

    def test_once_repairs_only_exact_rule_and_internet_gems(self) -> None:
        """Verify that a single repair changes only the recognized Internet service."""
        before = copy.deepcopy(self.state)
        result = self.invoke()
        self.assert_success(result)
        self.assertEqual(FIXED_RULE, self.state["mappers"]["pmapper1"]["rules"])
        self.assertEqual(["1084", "1086", "1087"], self.applied_gems())
        tc_writes = [m for m in self.state["mutations"] if m["kind"] == "tc_replace"]
        self.assertEqual(1, len(tc_writes))
        self.assertEqual(before["mappers"]["pmapper2"], self.state["mappers"]["pmapper2"])
        self.assertEqual(before["mappers"]["pmapper3"], self.state["mappers"]["pmapper3"])
        for gem in ("1072", "1073", "1080", "1081", "1082", "1083"):
            self.assertEqual(before["gems"][gem], self.state["gems"][gem])

    def test_native_pass_handle_two_is_preserved(self) -> None:
        """Verify that native pass handle two is preserved."""
        self.state = initial_state(NATIVE_RULE)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("rules=native", result.stdout)
        self.assertEqual(NATIVE_RULE, self.state["mappers"]["pmapper1"]["rules"])
        self.assertFalse(any(m["kind"] == "tc_replace" for m in self.state["mutations"]))
        self.assertEqual(["1084", "1086", "1087"], self.applied_gems())

    def test_healthy_configuration_is_unchanged(self) -> None:
        """Verify that healthy configuration is unchanged."""
        self.state = initial_state(FIXED_RULE)
        for gem in ("1084", "1085", "1086", "1087"):
            self.state["gems"][gem]["enc"] = 3
        result = self.invoke()
        self.assert_success(result)
        self.assertEqual([], self.state["mutations"])
        self.assertFalse(any(c[0] == "omci_pipe.sh" for c in self.state["calls"]))

    def test_all_changed_me_aliases_are_verified_before_any_mutation(self) -> None:
        """Validate every candidate's Port-ID before changing TC or encryption."""
        result = self.invoke()
        self.assert_success(result)
        first_write = next(
            index
            for index, call in enumerate(self.state["calls"])
            if call[:3] == ["tc", "filter", "replace"] or call[:2] == ["omci_pipe.sh", "meads"]
        )
        alias_reads = [
            call[3]
            for call in self.state["calls"][:first_write]
            if call[:3] == ["omci_pipe.sh", "meadg", "268"] and call[4] == "1"
        ]
        self.assertEqual(["1084", "1086", "1087"], alias_reads)
        writes = [mutation for mutation in self.state["mutations"] if mutation["kind"] == "omci_set"]
        self.assertTrue(all(write["gem"] == write["port"] for write in writes))

    def test_mismatched_late_me_alias_blocks_all_writes_in_both_modes(self) -> None:
        """Refuse a late mismatched ME before changing earlier GEMs or VLAN rules."""
        for encryption in (1, 3):
            with self.subTest(encryption=encryption):
                self.state = initial_state()
                self.state["omci_port_ids"] = {"1087": 1080}
                self.configure(encryption=encryption)
                before = copy.deepcopy(self.state["gems"])
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("instance 1087 refers to Port-ID 1080", result.stdout)
                self.assertIn("unsupported mapping", result.stdout)
                self.assertEqual([], self.state["mutations"])
                self.assertEqual(before, self.state["gems"])
                self.assertEqual(BAD_RULE, self.state["mappers"]["pmapper1"]["rules"])

    def test_me_port_id_accepts_exact_compact_or_spaced_hexadecimal(self) -> None:
        """Decode supported two-byte responses without treating them as decimal."""
        for reply in (
            "errorcode=0 attr_data=04 3C\n",
            "errorcode=0 attr_data=043c\n",
            " \terrorcode=0 attr_data=04\t3c \n",
        ):
            with self.subTest(reply=reply):
                self.state = initial_state(NATIVE_RULE)
                self.state["omci_port_replies"] = {"1084": reply}
                result = self.invoke()
                self.assert_success(result)
                self.assertEqual(["1084", "1086", "1087"], self.applied_gems())

    def test_invalid_or_failed_me_port_id_response_blocks_all_writes(self) -> None:
        """Reject missing, ambiguous, failed, and malformed attribute responses."""
        replies = (
            "",
            "errorcode=-16 attr_data=04 3C\n",
            "attr_data=04 3C\n",
            "errorcode=0 attr_data=\n",
            "errorcode=0 attr_data=04\n",
            "errorcode=0 attr_data=04 3C 00\n",
            "errorcode=0 attr_data=0x043C\n",
            "errorcode=0 attr_data=GG 3C\n",
            "errorcode=0 attr_data=04 3C PRIVATE_REPLY_VALUE\n",
            "errorcode=0 attr_data=04 3C errorcode=-16\n",
            "errorcode=0 attr_data=04 3C\nerrorcode=0 attr_data=04 3C\n",
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.state = initial_state()
                self.state["omci_port_replies"] = {"1087": reply}
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("invalid OMCI ME 268 instance 1087 Port-ID response", result.stdout)
                self.assertNotIn("PRIVATE_REPLY_VALUE", result.stdout + result.stderr)
                self.assertEqual([], self.state["mutations"])

    def test_me_port_id_command_failure_overrides_success_looking_output(self) -> None:
        """Require a successful read command as well as a valid wrapper response."""
        self.state["omci_port_failures"] = ["1087"]
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("cannot read OMCI ME 268 instance 1087 Port-ID", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_me_alias_is_checked_again_immediately_before_writing(self) -> None:
        """Stop when an ME starts referring to another service after preflight."""
        self.state = initial_state(NATIVE_RULE)
        self.state["omci_port_id_sequences"] = {"1084": [1084, 1080]}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("instance 1084 refers to Port-ID 1080", result.stdout)
        self.assertEqual([], self.state["mutations"])
        self.assertEqual(0, self.state["gems"]["1080"]["enc"])
        self.assertEqual(0, self.state["gems"]["1084"]["enc"])

    def test_service_swap_during_omci_reads_never_changes_the_old_service(self) -> None:
        """Revalidate VLAN ownership after both batch and immediate alias reads."""
        for read_number in ("1", "2"):
            with self.subTest(read_number=read_number):
                self.state = initial_state(NATIVE_RULE)
                replacement = copy.deepcopy(self.state["mappers"])
                replacement["pmapper1"]["rules"] = replacement["pmapper1"]["rules"].replace("835", "836")
                replacement["pmapper2"]["rules"] = replacement["pmapper2"]["rules"].replace("836", "835")
                self.state["omci_port_events"] = {"1084": {read_number: {"mappers": replacement}}}
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("selected service changed during inspection", result.stdout)
                self.assertEqual([], self.state["mutations"])
                self.assertEqual(0, self.state["gems"]["1084"]["enc"])
                self.assertEqual(0, self.state["gems"]["1080"]["enc"])

    def test_reprovisioned_members_or_mapper_index_invalidate_the_service(self) -> None:
        """Detect topology changes that preserve the mapper name and VLAN rule."""
        changed_members = copy.deepcopy(self.state["mappers"]["pmapper1"])
        changed_members["gems"] = [1080, 1081, 1082, 1083]
        changed_index = copy.deepcopy(self.state["mappers"]["pmapper1"])
        changed_index["ifindex"] = 146
        for mapper in (changed_members, changed_index):
            with self.subTest(mapper=mapper):
                self.state = initial_state()
                self.state["omci_port_events"] = {"1084": {"1": {"mappers": {"pmapper1": mapper}}}}
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("selected service changed during inspection", result.stdout)
                self.assertEqual([], self.state["mutations"])

    def test_new_ambiguous_service_during_preflight_blocks_all_mutations(self) -> None:
        """Refuse a newly introduced second selected-service mapper before writes."""
        second = copy.deepcopy(self.state["mappers"]["pmapper2"])
        second["rules"] = second["rules"].replace("836", "835")
        self.state["omci_port_events"] = {"1084": {"1": {"mappers": {"pmapper2": second}}}}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("multiple VLAN-835 mappers", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_service_change_after_tc_repair_prevents_encryption_writes(self) -> None:
        """Stop after a legitimate TC step if the service is reassigned afterward."""
        replacement = copy.deepcopy(self.state["mappers"])
        replacement["pmapper1"]["rules"] = NATIVE_RULE.replace("835", "836")
        replacement["pmapper2"]["rules"] = replacement["pmapper2"]["rules"].replace("836", "835")
        self.state["tc_mapper_events"] = {"835-repair": replacement}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.applied_gems())
        self.assertEqual(1, len(self.state["mutations"]))
        self.assertEqual("tc_replace", self.state["mutations"][0]["kind"])

    def test_watcher_requires_two_fresh_samples_after_service_invalidation(self) -> None:
        """Discard the settled sample when a blocking read exposes another service."""
        for passes, expected in ((3, []), (4, ["1080", "1081", "1082", "1083"])):
            with self.subTest(passes=passes):
                self.state = initial_state(NATIVE_RULE)
                replacement = copy.deepcopy(self.state["mappers"])
                replacement["pmapper1"]["rules"] = replacement["pmapper1"]["rules"].replace("835", "836")
                replacement["pmapper2"]["rules"] = replacement["pmapper2"]["rules"].replace("836", "835")
                self.state["omci_port_events"] = {"1084": {"1": {"mappers": replacement}}}
                result = self.invoke("--watch", passes=passes)
                self.assert_success(result)
                self.assertIn("selected service changed during inspection", result.stdout)
                self.assertIn("Waiting for stable VLAN 835: pmapper2", result.stdout)
                self.assertEqual(expected, self.applied_gems())

    def test_dynamic_gems_outside_both_old_and_current_allocations(self) -> None:
        """Verify that dynamic GEMs outside both old and current allocations."""
        self.state = initial_state(NATIVE_RULE)
        ids = [401, 9021, 45000]
        self.state["mappers"]["pmapper1"]["gems"] = ids
        self.state["gems"].update({str(gem): {"enc": 0} for gem in ids})
        result = self.invoke()
        self.assert_success(result)
        self.assertEqual([str(gem) for gem in ids], self.applied_gems())
        self.assertIn("GEMs=401 9021 45000", result.stdout)
        self.assertIn("no fixed GEM range", result.stdout)

    def test_invalid_gem_aborts_before_any_rule_or_gem_write(self) -> None:
        """Verify that invalid GEM aborts before any rule or GEM write."""
        for fields in ({"mc": 1}, {"dir": 2}, {"enc": 2}, {"enc": 9}, {"type": 1}):
            with self.subTest(fields=fields):
                self.state = initial_state()
                self.state["gems"]["1087"].update(fields)
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("not a supported unicast data GEM", result.stdout)
                self.assertEqual([], self.state["mutations"])

    def test_duplicate_vlan_835_mappers_abort_without_changes(self) -> None:
        """Verify that duplicate VLAN 835 mappers abort without changes."""
        self.state["mappers"]["pmapper2"]["rules"] = NATIVE_RULE.replace("pmapper1", "pmapper2")
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("multiple VLAN-835 mappers", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_unsupported_835_rules_abort_without_changes(self) -> None:
        """Verify that unsupported 835 rules abort without changes."""
        variants = [
            BAD_RULE.replace("modify id 836", "modify id 837"),
            BAD_RULE.replace("handle 0x1", "handle 0x9"),
            BAD_RULE.replace("  vlan_id 835", "  vlan_id 835\n  vlan_prio 7"),
            BAD_RULE.replace("  vlan_id 835", "  vlan_id 835\n  src_mac aa:bb:cc:dd:ee:ff"),
            BAD_RULE.replace("pref 1 flower", "pref 7 flower"),
            BAD_RULE.replace("         index 130", "        action order 2: gact action drop\n         index 130"),
            BAD_RULE + NATIVE_RULE,
        ]
        for rule in variants:
            with self.subTest(rule=rule):
                self.state = initial_state(rule)
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("unsupported VLAN-835 rule", result.stdout)
                self.assertEqual([], self.state["mutations"])

    def test_textual_omci_error_is_failure_despite_zero_shell_exit(self) -> None:
        """Verify that textual OMCI error is failure despite zero shell exit."""
        self.state = initial_state(NATIVE_RULE)
        self.state["omci_failures"] = ["1084"]
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("apply failed: errorcode=-16", result.stdout)
        self.assertEqual(["1084"], self.applied_gems())
        self.assertEqual(0, self.state["gems"]["1084"]["enc"])

    def test_omci_apply_errors_are_logged_without_raw_vendor_output(self) -> None:
        """Persist a canonical error code without disclosing opaque vendor data."""
        self.state = initial_state(NATIVE_RULE)
        self.state["omci_failures"] = ["1084"]
        self.state["omci_set_replies"] = {"1084": "errorcode=-16 PRIVATE-OMCI-FIXTURE\n"}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        log = (self.conf / "dei835.log").read_text()
        self.assertIn("apply failed: errorcode=-16", result.stdout)
        self.assertIn("apply failed: errorcode=-16", log)
        self.assertNotIn("PRIVATE-OMCI-FIXTURE", result.stdout + result.stderr + log)

    def test_omci_readback_errors_are_logged_without_raw_vendor_output(self) -> None:
        """Keep malformed attribute-response content out of output and saved logs."""
        self.state = initial_state(NATIVE_RULE)
        self.state["omci_readback_replies"] = {"1084": "errorcode=0 attr_data=00 PRIVATE-OMCI-FIXTURE\n"}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        log = (self.conf / "dei835.log").read_text()
        self.assertIn("readback failed: errorcode=0", result.stdout)
        self.assertNotIn("PRIVATE-OMCI-FIXTURE", result.stdout + result.stderr + log)

    def test_omci_success_without_changed_readback_is_failure(self) -> None:
        """Verify that OMCI success without changed readback is failure."""
        self.state = initial_state(NATIVE_RULE)
        self.state["omci_ignore_writes"] = ["1084"]
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("readback failed", result.stdout)
        self.assertEqual(["1084"], self.applied_gems())

    def test_tc_failure_stops_before_encryption_writes(self) -> None:
        """Verify that TC failure stops before encryption writes."""
        self.state["tc_failure"] = True
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.applied_gems())
        self.assertEqual(BAD_RULE, self.state["mappers"]["pmapper1"]["rules"])

    def test_tc_success_without_changed_readback_stops(self) -> None:
        """Verify that TC success without changed readback stops."""
        self.state["tc_ignore_write"] = True
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("VLAN repair readback failed", result.stdout)
        self.assertEqual([], self.applied_gems())

    def test_once_refuses_allocation_that_changes_during_settle(self) -> None:
        """Verify that once refuses allocation that changes during settle."""
        replacement = copy.deepcopy(self.state["mappers"]["pmapper1"])
        replacement["gems"] = [1084, 1085]
        self.state["events"] = {"1": {"mappers": {"pmapper1": replacement}}}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Provisioning changed", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_watch_requires_two_stable_passes(self) -> None:
        """Verify that watch requires two stable passes."""
        result = self.invoke("--watch", passes=1)
        self.assert_success(result)
        self.assertEqual([], self.state["mutations"])
        self.assertIn("Waiting for stable VLAN 835", result.stdout)
        self.assertFalse((self.run_dir / "worker.pid").exists())
        self.state = initial_state()
        result = self.invoke("--watch", passes=2)
        self.assert_success(result)
        self.assertEqual(["1084", "1086", "1087"], self.applied_gems())
        self.assertFalse((self.run_dir / "worker.pid").exists())

    def test_watch_recovers_later_encryption_reset_without_repeated_writes(self) -> None:
        """Verify that watch recovers later encryption reset without repeated writes."""
        self.state = initial_state(NATIVE_RULE)
        # Pass 2 applies; after that pass, simulated OLT resets just one GEM.
        self.state["events"] = {"2": {"encryption": {"1085": 0}}}
        result = self.invoke("--watch", passes=5)
        self.assert_success(result)
        self.assertEqual(["1084", "1086", "1087", "1085"], self.applied_gems())
        self.assertTrue(all(self.state["gems"][str(g)]["enc"] == DOWNSTREAM_ENCRYPTION for g in range(1084, 1088)))

    def test_watch_reallocates_gems_and_waits_for_new_stable_signature(self) -> None:
        """Verify that watch reallocates GEMs and waits for new stable signature."""
        self.state = initial_state(NATIVE_RULE)
        replacement = copy.deepcopy(self.state["mappers"]["pmapper1"])
        replacement["gems"] = [3210, 3211]
        replacement_gems = copy.deepcopy(self.state["gems"])
        replacement_gems.update({"3210": {"enc": 0}, "3211": {"enc": 0}})
        self.state["events"] = {"2": {"mappers": {"pmapper1": replacement}, "replace_gems": replacement_gems}}
        result = self.invoke("--watch", passes=5)
        self.assert_success(result)
        self.assertEqual(["1084", "1086", "1087", "3210", "3211"], self.applied_gems())

    def test_non_o5_session_is_not_modified(self) -> None:
        """Verify that non-O5 session is not modified."""
        self.state["pon_state"] = 40
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("waiting for PON O5", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_ambiguous_or_failed_pon_status_never_authorizes_changes(self) -> None:
        """Require one successful status record with one unambiguous current state."""
        replies = (
            "",
            "current=51 previous=40\n",
            "errorcode=-16 current=51\n",
            "errorcode=-16 current=51 errorcode=0\n",
            "errorcode=0 current=40 current=51\n",
            "errorcode=0 current=51 current=51\n",
            "errorcode=0\ncurrent=51\n",
            "errorcode=0 current=51\nerrorcode=0 current=40\n",
            "errorcode=0 current=51x\n",
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.state = initial_state()
                self.state["pon_reply"] = reply
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], self.state["mutations"])
        self.state = initial_state()
        self.state["pon_exit_code"] = 1
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.state["mutations"])

    def test_unresponsive_pon_status_is_bounded_without_device_mutations(self) -> None:
        """Stop a stalled read-only PON query without hanging runtime validation."""
        self.env["DEI835_PON_TIMEOUT_SECONDS"] = "1"
        pon = self.bin / "pon"
        pon.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, time\n"
            "pathlib.Path(os.environ['DEI835_MODULE_ROOT'], 'pon-started').touch()\n"
            "time.sleep(60)\n"
        )
        started = time.monotonic()
        result = self.invoke()
        elapsed = time.monotonic() - started
        self.assertNotEqual(0, result.returncode)
        self.assertIn("bounded interval", result.stdout)
        self.assertTrue((self.module / "pon-started").exists())
        self.assertLess(elapsed, 10)
        self.assertEqual([], self.state["mutations"])

    def test_runtime_pon_command_must_be_within_an_explicit_module_root(self) -> None:
        """Reject a host command when a different synthetic module root is selected."""
        unrelated = self.root / "other-module"
        unrelated.mkdir()
        self.env["DEI835_MODULE_ROOT"] = str(unrelated)
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("cannot locate a bounded module PON command", result.stdout)
        self.assertFalse(any(call[0] == "pon" for call in self.state["calls"]))
        self.assertEqual([], self.state["mutations"])

    def test_invalid_pon_timeout_is_rejected_before_running_vendor_commands(self) -> None:
        """Keep the read timeout finite and within the documented safety bound."""
        for timeout in ("0", "31", "unbounded"):
            with self.subTest(timeout=timeout):
                self.state = initial_state()
                self.env["DEI835_PON_TIMEOUT_SECONDS"] = timeout
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("invalid PON status timeout", result.stdout)
                self.assertFalse(any(call[0] == "pon" for call in self.state["calls"]))
                self.assertEqual([], self.state["mutations"])

    def test_once_requires_hook_only_mode(self) -> None:
        """Verify that once requires hook only mode."""
        self.state["fix_mode"] = 1
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.state["mutations"])

    def test_failed_mode_read_cannot_authorize_repair_from_partial_output(self) -> None:
        """Require a successful firmware read even when its output looks like hook-only mode."""
        for mode in ("--once", "--watch"):
            with self.subTest(mode=mode):
                self.state = initial_state()
                self.state["fix_mode"] = 2
                self.state["fix_mode_exit_code"] = 1
                result = self.invoke(mode, passes=2)
                if mode == "--once":
                    self.assertNotEqual(0, result.returncode)
                else:
                    self.assert_success(result)
                    self.assertIn("Cannot confirm hook-only mode", result.stdout)
                self.assertEqual([], self.state["mutations"])

    def test_watch_stops_on_fix_mode_change_before_writing(self) -> None:
        """Verify that watch stops on fix mode change before writing."""
        self.state["events"] = {"1": {"fix_mode": 0}}
        result = self.invoke("--watch", passes=5)
        self.assert_success(result)
        self.assertEqual([], self.state["mutations"])
        self.assertIn("mode changed to 0", result.stdout)
        self.assertEqual(1, self.state["sleeps"])
        self.assertFalse((self.run_dir / "worker.pid").exists())

    def test_watch_waits_for_unknown_mode_then_recovers_after_stable_passes(self) -> None:
        """Verify that watch waits for unknown mode then recovers after stable passes."""
        self.state["fix_mode"] = ""
        self.state["events"] = {"1": {"fix_mode": 2}}
        result = self.invoke("--watch", passes=3)
        self.assert_success(result)
        self.assertIn("Cannot confirm hook-only mode; waiting without changes", result.stdout)
        self.assertEqual(["1084", "1086", "1087"], self.applied_gems())
        self.assertEqual(FIXED_RULE, self.state["mappers"]["pmapper1"]["rules"])
        first_write = next(
            index for index, call in enumerate(self.state["calls"]) if call[:3] == ["tc", "filter", "replace"]
        )
        sleeps_before_write = [call for call in self.state["calls"][:first_write] if call[0] == "sleep"]
        self.assertEqual(2, len(sleeps_before_write))
        self.assertFalse((self.run_dir / "worker.pid").exists())

    def test_watch_stops_on_cooperative_stop_file_before_writing(self) -> None:
        """Verify that watch stops on cooperative stop file before writing."""
        self.state["events"] = {"1": {"stop": True}}
        result = self.invoke("--watch", passes=5)
        self.assert_success(result)
        self.assertEqual([], self.state["mutations"])
        self.assertIn("stopped on request", result.stdout)
        self.assertEqual(1, self.state["sleeps"])

    def test_once_refuses_recreated_mapper_even_when_gem_ids_match(self) -> None:
        """Verify that once refuses recreated mapper even when GEM ids match."""
        replacement = copy.deepcopy(self.state["mappers"]["pmapper1"])
        replacement["ifindex"] = 146
        self.state["events"] = {"1": {"mappers": {"pmapper1": replacement}}}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Provisioning changed", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_status_reads_actual_service_gems_and_never_changes_configuration(self) -> None:
        """Verify that status reads actual service GEMs and never changes configuration."""
        self.state = initial_state(NATIVE_RULE)
        # Avoid conflating an absent optional log with device inspection.
        (self.conf / "dei835.log").write_text("test fixture\n")
        result = self.invoke("", script="status.sh")
        self.assert_success(result)
        self.assertEqual([], self.state["mutations"])
        queried = [call[2] for call in self.state["calls"] if call[:2] == ["pon", "gpcg"]]
        self.assertEqual(["1084", "1085", "1086", "1087"], queried)
        self.assertIn("Watcher: not running", result.stdout)

    def test_status_returns_failure_for_a_failed_pon_query(self) -> None:
        """Report failed PON inspection without claiming a successful status check."""
        self.state["pon_exit_code"] = 1
        result = self.invoke("", script="status.sh")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("PON status unavailable", result.stderr)
        self.assertEqual([], self.state["mutations"])

    def test_status_rejects_failed_counter_response_with_successful_shell_exit(self) -> None:
        """Recognize vendor counter errors even when the process exits successfully."""
        self.state["pon_counter_replies"] = {"1084": "errorcode=-16\n"}
        result = self.invoke("", script="status.sh")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("GEM 1084 counters unavailable", result.stderr)
        self.assertIn("gem_port_id=1087", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_status_bounds_a_hanging_gem_counter_query(self) -> None:
        """Bound each counter read and continue reporting the remaining GEMs."""
        self.env["DEI835_PON_TIMEOUT_SECONDS"] = "1"
        pon = self.bin / "pon"
        contents = pon.read_text()
        needle = '            assert len(args) == 2 and args[0] == "gpcg", args\n'
        self.assertEqual(1, contents.count(needle))
        pon.write_text(
            contents.replace(
                needle,
                needle
                + '            if args[1] == "1084":\n                import time\n                time.sleep(60)\n',
                1,
            )
        )
        started = time.monotonic()
        result = self.invoke("", script="status.sh")
        elapsed = time.monotonic() - started
        self.assertNotEqual(0, result.returncode)
        self.assertIn("GEM 1084 counters unavailable", result.stderr)
        self.assertIn("gem_port_id=1087", result.stdout)
        self.assertLess(elapsed, 10)
        self.assertEqual([], self.state["mutations"])

    def test_native_836_selects_only_its_own_gems_and_preserves_835(self) -> None:
        """Verify that selecting VLAN 836 preserves the other Internet service."""
        self.configure(vlan=836)
        before = copy.deepcopy(self.state)
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("VLAN 836", result.stdout)
        self.assertEqual(["1080", "1081", "1082", "1083"], self.applied_gems())
        self.assertEqual(before["mappers"], self.state["mappers"])
        self.assertFalse(any(m["kind"] == "tc_replace" for m in self.state["mutations"]))

    def test_legacy_836_is_normalized_in_both_directions(self) -> None:
        """Verify that legacy 836 is normalized in both directions."""
        self.configure(vlan=836)
        self.legacy_836()
        before = copy.deepcopy(self.state)
        result = self.invoke()
        self.assert_success(result)
        steps = [m["step"] for m in self.state["mutations"] if m["kind"] == "tc_replace"]
        self.assertEqual(["836-allow", "836-keep", "836-drop"], steps)
        self.assertEqual(["1080", "1081", "1082", "1083"], self.applied_gems())
        mapper = self.state["mappers"]["pmapper2"]
        self.assertIn("vlan_id 836", mapper["rules"])
        self.assertNotIn("vlan  push", mapper["rules"])
        self.assertNotIn("vlan  pop", mapper["ingress"])
        self.assertEqual(before["mappers"]["pmapper1"], self.state["mappers"]["pmapper1"])
        self.state["mutations"] = []
        result = self.invoke()
        self.assert_success(result)
        self.assertIn("legacy836-keep-allow-drop", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_partial_836_normalization_retries_to_completion(self) -> None:
        """Verify that partial 836 normalization retries to completion."""
        self.configure(vlan=836)
        for failing in ("836-allow", "836-keep", "836-drop"):
            with self.subTest(failing=failing):
                self.state = initial_state()
                self.legacy_836()
                self.state["tc_fail_on"] = failing
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], self.applied_gems())
                self.state.pop("tc_fail_on")
                self.state["mutations"] = []
                result = self.invoke()
                self.assert_success(result)
                self.assertEqual(["1080", "1081", "1082", "1083"], self.applied_gems())
                self.assertNotIn("vlan  pop", self.state["mappers"]["pmapper2"]["ingress"])
                self.assertNotIn("vlan  push", self.state["mappers"]["pmapper2"]["rules"])

    def test_836_never_uses_835_rewrite_as_service_identity(self) -> None:
        """Verify that an output tag alone cannot identify the VLAN-836 service."""
        self.configure(vlan=836)
        self.state["mappers"].pop("pmapper2")
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("waiting for VLAN-836 service mapper", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_unknown_legacy_836_matches_are_not_modified(self) -> None:
        """Verify that unknown legacy 836 matches are not modified."""
        self.configure(vlan=836)
        self.legacy_836()
        self.state["mappers"]["pmapper2"]["rules"] = LEGACY_836_EGRESS.replace(
            "  skip_sw", "  skip_sw\n  src_mac aa:bb:cc:dd:ee:ff", 1
        )
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unsupported VLAN-836", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_duplicate_native_and_legacy_836_mappers_abort(self) -> None:
        """Verify that duplicate native and legacy 836 mappers abort."""
        self.configure(vlan=836)
        self.legacy_836()
        self.state["mappers"]["pmapper3"]["rules"] = NATIVE_RULE.replace("835", "836")
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("multiple VLAN-836", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_normalization_can_be_disabled_independently_of_encryption(self) -> None:
        """Verify that normalization can be disabled independently of encryption."""
        self.configure(vlan=836, normalize=False)
        self.legacy_836()
        before = copy.deepcopy(self.state["mappers"])
        result = self.invoke()
        self.assert_success(result)
        self.assertEqual(before, self.state["mappers"])
        self.assertEqual(["1080", "1081", "1082", "1083"], self.applied_gems())

    def test_encryption_one_is_applied_only_to_selected_unicast_gems(self) -> None:
        """Verify that encryption one is applied only to selected unicast GEMs."""
        self.configure(encryption=1)
        result = self.invoke()
        self.assert_success(result)
        self.assertEqual(["1084", "1085", "1086", "1087"], self.applied_gems())
        self.assertTrue(all(m["value"] == 1 for m in self.state["mutations"] if m["kind"] == "omci_set"))
        self.assertEqual(0, self.state["gems"]["1081"]["enc"])

    def test_explicit_missing_config_never_falls_back(self) -> None:
        """Verify that explicit missing config never falls back."""
        self.env["DEI835_CONFIG_PATH"] = str(self.root / "missing.toml")
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("explicit runtime configuration", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_invalid_stored_config_never_falls_back(self) -> None:
        """Verify that invalid stored config never falls back."""
        (self.conf / "dei-pon.toml").write_text("schema_version = 1\n[internet]\nvlan = 999\n")
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.state["mutations"])

    def test_config_change_during_settle_aborts(self) -> None:
        """Verify that config change during settle aborts."""
        self.configure(vlan=835)
        self.state["events"] = {"1": {"config_text": "schema_version = 1\n[internet]\nvlan = 836\n"}}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Provisioning changed", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_disabled_watcher_exits_without_device_writes(self) -> None:
        """Verify that disabled watcher exits without device writes."""
        self.configure(enabled=False)
        result = self.invoke("--watch", passes=3)
        self.assert_success(result)
        self.assertIn("disabled by configuration", result.stdout)
        self.assertEqual([], self.state["mutations"])
        self.assertEqual(0, self.state["sleeps"])

    def test_watcher_reloads_service_choice_and_interval(self) -> None:
        """Verify that watcher reloads service choice and interval."""
        self.configure(vlan=835, interval=15)
        changed = "schema_version = 1\n[internet]\nvlan = 836\n[watcher]\ninterval_seconds = 20\n"
        self.state["events"] = {"2": {"config_text": changed}}
        result = self.invoke("--watch", passes=5)
        self.assert_success(result)
        self.assertEqual(["1084", "1086", "1087", "1080", "1081", "1082", "1083"], self.applied_gems())
        intervals = [call[1] for call in self.state["calls"] if call[0] == "sleep"]
        self.assertEqual(["15", "15", "20", "20"], intervals)

    def test_watcher_recovers_from_temporary_invalid_config(self) -> None:
        """Verify that watcher recovers from temporary invalid config."""
        self.configure()
        self.state["events"] = {
            "1": {"config_text": "schema_version = 1\n[internet]\nvlan = 999\n"},
            "2": {"config_text": "schema_version = 1\n[internet]\nvlan = 835\n"},
        }
        result = self.invoke("--watch", passes=4)
        self.assert_success(result)
        self.assertIn("invalid runtime configuration", result.stdout)
        self.assertEqual(["1084", "1086", "1087"], self.applied_gems())

    def test_failsafe_blocks_runtime_even_with_o5_and_a_known_mismatch(self) -> None:
        """Verify that failsafe blocks runtime even with O5 and a known mismatch."""
        module = self.root / "module"
        (module / "ptconf").mkdir(parents=True)
        (module / "ptconf" / ".failsafe").touch()
        self.env["DEI835_MODULE_ROOT"] = str(module)
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("firmware failsafe", result.stdout)
        self.assertEqual([], self.state["mutations"])
        self.assertFalse(any(c[0] == "omci_pipe.sh" for c in self.state["calls"]))

    def test_failsafe_appearing_during_settle_blocks_all_writes(self) -> None:
        """Verify that failsafe appearing during settle blocks all writes."""
        module = self.root / "module"
        (module / "ptconf").mkdir(parents=True)
        self.env["DEI835_MODULE_ROOT"] = str(module)
        self.state["events"] = {"1": {"failsafe": True}}
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("firmware failsafe", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_healthy_once_logs_discovery_without_device_writes(self) -> None:
        """Record every selected GEM even when repair has nothing to change."""
        self.state = initial_state(FIXED_RULE)
        for gem in self.state["mappers"]["pmapper1"]["gems"]:
            self.state["gems"][str(gem)]["enc"] = DOWNSTREAM_ENCRYPTION
        result = self.invoke()
        self.assert_success(result)
        self.assertEqual([], self.state["mutations"])
        log = (self.conf / "dei835.log").read_text()
        self.assertIn("VLAN 835: mapper=pmapper1", log)
        self.assertIn("GEMs=1084 1085 1086 1087", log)
        for gem in (1084, 1085, 1086, 1087):
            self.assertIn(f"GEM {gem}: role=unicast-data", log)
        self.assertIn("encryption=3 target-encryption=3", log)
        self.assertEqual(0o600, (self.conf / "dei835.log").stat().st_mode & 0o777)

    def test_runtime_log_symlinks_block_writes_without_touching_targets(self) -> None:
        """Refuse either log-path symlink before any device mutation occurs."""
        target = self.root / "private-target"
        target.write_text("PRIVATE-LOG-FIXTURE\n")
        for name in ("dei835.log", "dei835.log.old"):
            with self.subTest(name=name):
                self.state = initial_state()
                link = self.conf / name
                link.symlink_to(target)
                result = self.invoke()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("runtime log paths must not be symlinks", result.stderr)
                self.assertEqual([], self.state["mutations"])
                self.assertEqual("PRIVATE-LOG-FIXTURE\n", target.read_text())
                self.assertNotIn("PRIVATE-LOG-FIXTURE", result.stdout + result.stderr)
                link.unlink()

    def test_nonregular_runtime_log_blocks_once_and_watch_before_mutations(self) -> None:
        """Propagate an unusable log path instead of reporting a successful repair."""
        (self.conf / "dei835.log").mkdir()
        for mode in ("--once", "--watch"):
            with self.subTest(mode=mode):
                self.state = initial_state()
                result = self.invoke(mode, passes=2)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("runtime log paths must be regular files", result.stderr)
                self.assertEqual([], self.state["mutations"])

    def test_runtime_log_rotation_preserves_private_permissions(self) -> None:
        """Retain the old log while making both current and rotated files private."""
        self.state = initial_state(NATIVE_RULE)
        previous = "old runtime record\n" * 2000
        log = self.conf / "dei835.log"
        log.write_text(previous)
        log.chmod(0o644)
        result = self.invoke()
        self.assert_success(result)
        old = self.conf / "dei835.log.old"
        self.assertEqual(previous, old.read_text())
        self.assertEqual(0o600, old.stat().st_mode & 0o777)
        self.assertEqual(0o600, log.stat().st_mode & 0o777)

    def test_failed_runtime_append_retains_pending_records_and_returns_failure(self) -> None:
        """Preserve buffered records if append fails after the log-path preflight."""
        assert BUSYBOX is not None
        # Change the destination between the log-size check and the append.
        script = r"""
. "$DEI835_CONF_DIR/dei-835-lib.sh"
DEI_LOG_BUFFER='retained audit fixture'
date() {
    mv "$DEI835_CONF_DIR/dei835.log" "$DEI835_CONF_DIR/before-append" || return 1
    mkdir "$DEI835_CONF_DIR/dei835.log" || return 1
    printf 'test timestamp\n'
}
dei_flush_log && exit 90
[ "$DEI_LOG_BUFFER" = 'retained audit fixture' ] || exit 91
printf '%s\n' "$DEI_LOG_BUFFER"
"""
        result = subprocess.run(
            [BUSYBOX, "ash", "-c", script],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assert_success(result)
        self.assertIn("cannot persist the runtime log", result.stderr)
        self.assertEqual("retained audit fixture\n", result.stdout)

    def test_stable_watcher_logs_full_discovery_only_once(self) -> None:
        """Avoid repeating the full GEM snapshot during unchanged polling passes."""
        self.state = initial_state(NATIVE_RULE)
        result = self.invoke("--watch", passes=5)
        self.assert_success(result)
        log = (self.conf / "dei835.log").read_text()
        self.assertEqual(1, log.count("Discovery source:"))
        for gem in (1084, 1085, 1086, 1087):
            self.assertEqual(1, log.count(f"GEM {gem}: role=unicast-data"))

    def test_watcher_logs_each_new_allocation_after_it_settles(self) -> None:
        """Report all members when the OLT supplies a new noncontiguous allocation."""
        self.state = initial_state(NATIVE_RULE)
        replacement = copy.deepcopy(self.state["mappers"]["pmapper1"])
        replacement["gems"] = [401, 9021, 45000]
        replacement_gems = copy.deepcopy(self.state["gems"])
        replacement_gems.update({str(gem): {"enc": 0} for gem in replacement["gems"]})
        self.state["events"] = {"2": {"mappers": {"pmapper1": replacement}, "replace_gems": replacement_gems}}
        result = self.invoke("--watch", passes=6)
        self.assert_success(result)
        log = (self.conf / "dei835.log").read_text()
        self.assertEqual(2, log.count("Discovery source:"))
        self.assertIn("GEMs=401 9021 45000", log)
        for gem in replacement["gems"]:
            self.assertIn(f"GEM {gem}: role=unicast-data driver-id={gem}", log)

    def test_blocked_gem_validation_reports_actual_roles_without_writes(self) -> None:
        """Explain an unsupported member while retaining the complete discovery snapshot."""
        self.state["gems"]["1087"]["mc"] = 1
        result = self.invoke("--check")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("GEMs=1084 1085 1086 1087", result.stdout)
        self.assertIn("GEM 1087: role=multicast", result.stdout)
        self.assertIn("multicast=1", result.stdout)
        self.assertIn("Discovery blocked:", result.stdout)
        self.assertEqual([], self.state["mutations"])
        self.assertFalse((self.conf / "dei835.log").exists())

    def test_ambiguous_discovery_does_not_report_a_selected_mapper(self) -> None:
        """Avoid presenting the first candidate as selected when discovery is ambiguous."""
        self.state["mappers"]["pmapper2"]["rules"] = NATIVE_RULE.replace("pmapper1", "pmapper2")
        result = self.invoke("--check")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("GEM discovery unavailable:", result.stdout)
        self.assertNotIn("mapper=pmapper1", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_unchanged_blocked_discovery_is_not_repeated_in_watch_log(self) -> None:
        """Log a persistent unsupported GEM once rather than on every poll."""
        self.state["gems"]["1087"]["mc"] = 1
        result = self.invoke("--watch", passes=4)
        self.assert_success(result)
        log = (self.conf / "dei835.log").read_text()
        self.assertEqual(1, log.count("Discovery source:"))
        self.assertEqual(1, log.count("GEM 1087: role=multicast"))
        self.assertEqual([], self.state["mutations"])

    def test_unavailable_mapper_still_reports_inventory_without_selecting_targets(self) -> None:
        """Show observed numeric interfaces even when no service mapper can be chosen."""
        self.state["mappers"] = {}
        result = self.invoke("--check")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Observed numeric GEM interfaces (all services; not an apply target):", result.stdout)
        self.assertIn("1072 1073", result.stdout)
        self.assertIn("2046", result.stdout)
        self.assertNotIn("role=unicast-data", result.stdout)
        self.assertEqual([], self.state["mutations"])
        self.assertFalse((self.conf / "dei835.log").exists())

    def test_status_reads_recognized_installation_audit_without_modifying_it(self) -> None:
        """Include the latest installer findings while preserving the audit file."""
        audit = "DEI-PON-INSTALL-AUDIT-v1\nAction: install\nSelected GEMs: 1084 1085 1086 1087\n"
        (self.conf / "install.log").write_text(audit)
        result = self.invoke("", script="status.sh")
        self.assert_success(result)
        self.assertIn("Latest installation audit:", result.stdout)
        self.assertIn("Selected GEMs: 1084 1085 1086 1087", result.stdout)
        self.assertEqual(audit, (self.conf / "install.log").read_text())
        self.assertEqual([], self.state["mutations"])

    def test_status_does_not_dump_unrecognized_installation_log(self) -> None:
        """Avoid displaying unrelated file contents as an installer-generated audit."""
        (self.conf / "install.log").write_text("foreign log\nprivate fixture value\n")
        result = self.invoke("", script="status.sh")
        self.assert_success(result)
        self.assertIn("unrecognized format", result.stdout)
        self.assertNotIn("private fixture value", result.stdout)
        self.assertEqual([], self.state["mutations"])

    def test_status_never_follows_managed_log_symlinks(self) -> None:
        """Keep unrelated target contents private when a managed log is a symlink."""
        target = self.root / "private-status-target"
        target.write_text("DEI-PON-INSTALL-AUDIT-v1\nPRIVATE-STATUS-FIXTURE\n")
        for name in ("dei835.log", "install.log"):
            with self.subTest(name=name):
                link = self.conf / name
                link.symlink_to(target)
                result = self.invoke("", script="status.sh")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("not a regular managed file", result.stdout)
                self.assertNotIn("PRIVATE-STATUS-FIXTURE", result.stdout + result.stderr)
                self.assertEqual("DEI-PON-INSTALL-AUDIT-v1\nPRIVATE-STATUS-FIXTURE\n", target.read_text())
                self.assertEqual([], self.state["mutations"])
                link.unlink()

    def test_status_refuses_a_nonregular_managed_log(self) -> None:
        """Report an invalid log destination without opening or changing it."""
        (self.conf / "dei835.log").mkdir()
        result = self.invoke("", script="status.sh")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not a regular managed file", result.stdout)
        self.assertEqual([], self.state["mutations"])


if __name__ == "__main__":
    unittest.main()
