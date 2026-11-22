"""Offline schema and safety tests for the dependency-free TOML loader."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from paths import BUSYBOX, CONFIG, SOURCE

DEFAULTS = {
    "CFG_SCHEMA_VERSION": "1",
    "CFG_INTERNET_VLAN": "835",
    "CFG_INTERNET_ENCRYPTION_KEY_RING": "3",
    "CFG_INTERNET_NORMALIZE_VLAN": "true",
    "CFG_WATCHER_ENABLED": "true",
    "CFG_WATCHER_INTERVAL_SECONDS": "10",
    "CFG_LIBRARY_PATCH_ENABLED": "false",
    "CFG_LIBRARY_PATCH_POLICY": "auto",
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
}


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class ConfigTests(unittest.TestCase):
    """Exercise strict configuration parsing without executing input as code."""

    def setUp(self) -> None:
        """Create a private temporary configuration directory."""
        self.temporary = tempfile.TemporaryDirectory(prefix="dei-config-test-")
        self.root = Path(self.temporary.name)
        self.config = self.root / "settings.toml"

    def tearDown(self) -> None:
        """Remove the temporary files created for this test."""
        self.temporary.cleanup()

    def load(
        self,
        contents: str | bytes | None = None,
        *,
        path: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[bytes], dict[str, str]]:
        """Load TOML through BusyBox without evaluating its contents as shell.

        Returns:
            The captured process result and decoded configuration fields.

        """
        assert BUSYBOX is not None
        if contents is not None:
            self.config.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
            self.config.chmod(0o600)
        config_path = path or self.config
        # Fixed variable names only. Config text is never shell source.
        outputs = "\n".join("printf '%s\\000' \"$" + name + '"' for name in DEFAULTS)
        launcher = '. "$1" || exit 90\ndei_config_load "$2" || exit $?\n' + outputs
        result = subprocess.run(
            [BUSYBOX, "ash", "-c", launcher, "offline-config", str(SOURCE / "dei-config.sh"), str(config_path)],
            cwd=self.root,
            env=os.environ.copy(),
            capture_output=True,
            timeout=10,
            check=False,
        )
        values: dict[str, str] = {}
        if result.returncode == 0:
            parts = result.stdout.split(b"\0")
            self.assertEqual(b"", parts.pop(), result.stdout)
            self.assertEqual(len(DEFAULTS), len(parts), result.stdout)
            values = dict(zip(DEFAULTS, (part.decode() for part in parts), strict=True))
        return result, values

    def assert_valid(self, contents: str | bytes, **overrides: str) -> dict[str, str]:
        """Require successful parsing with exactly the expected field values.

        Returns:
            The parsed fields after checking defaults and requested overrides.

        """
        result, values = self.load(contents)
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(b"", result.stderr)
        self.assertEqual({**DEFAULTS, **overrides}, values)
        return values

    def assert_invalid(self, contents: str | bytes) -> subprocess.CompletedProcess[bytes]:
        """Require rejection without exposing parsed configuration values.

        Returns:
            The failed process result for further error assertions.

        """
        result, values = self.load(contents)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn(b"Configuration error", result.stderr)
        self.assertEqual({}, values)
        self.assertEqual(b"", result.stdout)
        return result

    def test_minimal_schema_uses_documented_defaults(self) -> None:
        """Verify a schema-only file supplies every documented default."""
        self.assert_valid("schema_version = 1\n")

    def test_shipped_example_matches_defaults(self) -> None:
        """Verify the tracked example agrees with parser defaults."""
        self.assert_valid((CONFIG / "dei-pon.example.toml").read_text())

    def test_all_fields_accept_valid_values(self) -> None:
        """Verify every supported field preserves its requested value."""
        self.assert_valid(
            """schema_version = 1
[internet]
vlan = 836
encryption_key_ring = 1
normalize_vlan = false
[watcher]
enabled = false
interval_seconds = 300
[library_patch]
enabled = true
policy = 'never'
[onu]
serial = "ZTEG1234abcd"
omcc_version = "0xA3"
vendor_id = 'ZTEG'
equipment_id = 'ZXHN F8648P'
hardware_version = 'V1.0'
mib_profile = 'dei_0205'
[management]
ip_address = '192.0.2.1'
netmask = '255.255.255.0'
gateway = '192.0.2.2'
ping_ip = '198.51.100.53'
failsafe_delay = 300
[storage]
persistent_root = 'enabled'
""",
            CFG_INTERNET_VLAN="836",
            CFG_INTERNET_ENCRYPTION_KEY_RING="1",
            CFG_INTERNET_NORMALIZE_VLAN="false",
            CFG_WATCHER_ENABLED="false",
            CFG_WATCHER_INTERVAL_SECONDS="300",
            CFG_LIBRARY_PATCH_ENABLED="true",
            CFG_LIBRARY_PATCH_POLICY="never",
            CFG_ONU_SERIAL="ZTEG1234abcd",
            CFG_ONU_OMCC_VERSION="0xA3",
            CFG_ONU_VENDOR_ID="ZTEG",
            CFG_ONU_EQUIPMENT_ID="ZXHN F8648P",
            CFG_ONU_HARDWARE_VERSION="V1.0",
            CFG_ONU_MIB_PROFILE="dei_0205",
            CFG_MANAGEMENT_IP_ADDRESS="192.0.2.1",
            CFG_MANAGEMENT_NETMASK="255.255.255.0",
            CFG_MANAGEMENT_GATEWAY="192.0.2.2",
            CFG_MANAGEMENT_PING_IP="198.51.100.53",
            CFG_MANAGEMENT_FAILSAFE_DELAY="300",
            CFG_STORAGE_PERSISTENT_ROOT="enabled",
        )

    def test_comments_hash_in_string_and_whitespace_are_preserved(self) -> None:
        """Distinguish comments from literal hashes and quoted whitespace."""
        self.assert_valid(
            "\t schema_version\t=\t1 # version\n[onu] # identity\nequipment_id = ' box # 1 ' # this is a comment\n",
            CFG_ONU_EQUIPMENT_ID=" box # 1 ",
        )

    def test_windows_newlines_are_accepted(self) -> None:
        """Accept CRLF formatting without changing selected values."""
        self.assert_valid(b"schema_version = 1\r\n[internet]\r\nvlan = 836\r\n", CFG_INTERNET_VLAN="836")

    def test_load_does_not_write_configuration_or_other_files(self) -> None:
        """Verify parsing leaves file contents, metadata, and directory entries intact."""
        contents = "schema_version = 1\n"
        self.config.write_text(contents)
        self.config.chmod(0o600)
        before = self.config.stat()
        result, _ = self.load()
        self.assertEqual(0, result.returncode, result.stderr)
        after = self.config.stat()
        self.assertEqual(contents, self.config.read_text())
        self.assertEqual(before.st_ino, after.st_ino)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_mode, after.st_mode)
        self.assertEqual([self.config], list(self.root.iterdir()))

    def test_shell_looking_strings_are_literal_data(self) -> None:
        """Keep command substitutions and shell separators as literal field data."""
        self.assert_valid(
            "schema_version=1\n[onu]\nequipment_id='$(touch PWNED)'\nhardware_version=';touch OTHER'\n",
            CFG_ONU_EQUIPMENT_ID="$(touch PWNED)",
            CFG_ONU_HARDWARE_VERSION=";touch OTHER",
        )
        self.assertFalse((self.root / "PWNED").exists())
        self.assertFalse((self.root / "OTHER").exists())

    def test_backtick_string_is_literal_data(self) -> None:
        """Keep legacy command-substitution syntax inert inside quoted data."""
        payload = chr(96) + "touch PWNED" + chr(96)
        self.assert_valid(
            f"schema_version=1\n[onu]\nequipment_id='{payload}'\n",
            CFG_ONU_EQUIPMENT_ID=payload,
        )
        self.assertFalse((self.root / "PWNED").exists())

    def test_unquoted_shell_payload_is_rejected_without_execution(self) -> None:
        """Reject unquoted command syntax before it can create a file."""
        self.assert_invalid("schema_version=1\n[internet]\nvlan=$(touch PWNED)\n")
        self.assertFalse((self.root / "PWNED").exists())

    def test_invalid_serial_is_never_echoed_in_error(self) -> None:
        """Report an invalid identity without including its supplied value."""
        invalid_serial = "ZTEGsecret-value"
        result = self.assert_invalid(f"schema_version=1\n[onu]\nserial='{invalid_serial}'\n")
        self.assertNotIn(invalid_serial.encode(), result.stdout + result.stderr)

    def test_schema_version_is_explicit_and_supported(self) -> None:
        """Require an explicit schema version with the supported integer value."""
        for contents in ("", "# empty\n", "[internet]\nvlan=835\n", "schema_version=2\n", 'schema_version="1"\n'):
            with self.subTest(contents=contents):
                self.assert_invalid(contents)

    def test_unknown_sections_fields_and_duplicate_entries_are_rejected(self) -> None:
        """Reject misspelled or repeated configuration fields and sections."""
        cases = (
            "schema_version=1\n[typo]\n",
            "schema_version=1\n[internet]\nvlna=835\n",
            "schema_version=1\nunknown=1\n",
            "schema_version=1\nschema_version=1\n",
            "schema_version=1\n[internet]\nvlan=835\nvlan=836\n",
            "schema_version=1\n[internet]\nvlan=835\n[internet]\nencryption_key_ring=3\n",
        )
        for contents in cases:
            with self.subTest(contents=contents):
                self.assert_invalid(contents)

    def test_unsupported_toml_syntax_is_rejected(self) -> None:
        """Reject TOML constructs outside the documented parser subset."""
        cases = (
            'schema_version=1\n"internet.vlan"=835\n',
            "schema_version=1\ninternet.vlan=835\n",
            "schema_version=1\n[[internet]]\n",
            "schema_version=1\n[internet.nested]\n",
            "schema_version=1\n[internet]\nvlan=[835]\n",
            "schema_version=1\n[internet]\nvlan={id=835}\n",
            'schema_version=1\n[onu]\nequipment_id="""multiline"""\n',
            "schema_version=1\n[onu]\nequipment_id='''multiline'''\n",
            "schema_version=1\n[onu]\nequipment_id='unterminated\n",
            "schema_version=1\n[onu]\nequipment_id='a' 'b'\n",
            "schema_version=1\n[onu]\nequipment_id='a'\n'b'\n",
        )
        for contents in cases:
            with self.subTest(contents=contents):
                self.assert_invalid(contents)

    def test_escapes_and_backslashes_are_rejected(self) -> None:
        """Reject escape sequences instead of silently interpreting them."""
        for value in ('"a\\nb"', '"a\\"b"', "'a\\b'", '"\\u0041"'):
            with self.subTest(value=value):
                self.assert_invalid(f"schema_version=1\n[onu]\nequipment_id={value}\n")

    def test_control_bytes_are_rejected_even_in_comments(self) -> None:
        """Reject control bytes regardless of their apparent TOML context."""
        for byte in (0, 1, 8, 11, 12, 14, 31, 127):
            with self.subTest(byte=byte):
                self.assert_invalid(b"schema_version=1\n# bad " + bytes([byte]) + b"\n")
        self.assert_invalid("schema_version=1\n[onu]\nequipment_id='tab\there'\n")
        self.assert_invalid("schema_version=1\n[onu]\nequipment_id='bad\rhere'\n")

    def test_decimal_integer_syntax_and_bounds_are_strict(self) -> None:
        """Reject unsupported integer representations and service identifiers."""
        for value in (
            "0835",
            "+835",
            "-835",
            "0x343",
            "8_35",
            "835.0",
            "8.35e2",
            "NaN",
            "99999999999999999999",
            "true",
            "'835'",
        ):
            with self.subTest(value=value):
                self.assert_invalid(f"schema_version=1\n[internet]\nvlan={value}\n")
        for value in (0, 834, 837, 4095):
            with self.subTest(value=value):
                self.assert_invalid(f"schema_version=1\n[internet]\nvlan={value}\n")
        for value in (0, 2, 4):
            with self.subTest(value=value):
                self.assert_invalid(f"schema_version=1\n[internet]\nencryption_key_ring={value}\n")

    def test_boolean_types_are_strict(self) -> None:
        """Reject numeric, quoted, or differently cased boolean substitutes."""
        for section in ("watcher", "library_patch"):
            for value in ("1", "0", "True", "FALSE", "'true'", "yes"):
                with self.subTest(section=section, value=value):
                    self.assert_invalid(f"schema_version=1\n[{section}]\nenabled={value}\n")

    def test_legacy_auto_policy_does_not_authorize_library_patching(self) -> None:
        """Keep a legacy auto policy disabled until an explicit opt-in is supplied."""
        self.assert_valid("schema_version=1\n[library_patch]\npolicy='auto'\n")

    def test_library_patch_opt_in_is_independent_of_policy(self) -> None:
        """Parse explicit opt-in without changing the separately selected policy."""
        self.assert_valid(
            "schema_version=1\n[library_patch]\nenabled=true\n",
            CFG_LIBRARY_PATCH_ENABLED="true",
        )
        self.assert_valid(
            "schema_version=1\n[library_patch]\nenabled=false\npolicy='never'\n",
            CFG_LIBRARY_PATCH_POLICY="never",
        )

    def test_watcher_and_failsafe_boundaries(self) -> None:
        """Accept boundary timings and reject values outside allowed ranges."""
        self.assert_valid("schema_version=1\n[watcher]\ninterval_seconds=5\n", CFG_WATCHER_INTERVAL_SECONDS="5")
        self.assert_valid("schema_version=1\n[management]\nfailsafe_delay=0\n", CFG_MANAGEMENT_FAILSAFE_DELAY="0")
        for value in (0, 4, 301):
            with self.subTest(interval=value):
                self.assert_invalid(f"schema_version=1\n[watcher]\ninterval_seconds={value}\n")
        for value in (301, 600, 601):
            with self.subTest(failsafe_delay=value):
                self.assert_invalid(f"schema_version=1\n[management]\nfailsafe_delay={value}\n")

    def test_omcc_version_matches_firmware_range_and_casing(self) -> None:
        """Normalize accepted hexadecimal bytes so firmware never substitutes its default."""
        for omcc in ("0x82", "0x8a", "0xa0", "0xbF", "0xBF"):
            with self.subTest(omcc=omcc):
                self.assert_valid(
                    f"schema_version=1\n[onu]\nomcc_version='{omcc}'\n",
                    CFG_ONU_OMCC_VERSION="0x" + omcc[2:].upper(),
                )
        for omcc in ("0x00", "0x80", "0x81", "0xC0", "0xFF"):
            with self.subTest(omcc=omcc):
                result = self.assert_invalid(f"schema_version=1\n[onu]\nomcc_version='{omcc}'\n")
                self.assertIn(b"0x82..0xBF", result.stderr)

    def test_enum_values_are_validated(self) -> None:
        """Accept supported policy choices and reject unknown alternatives."""
        self.assert_valid("schema_version=1\n[onu]\nmib_profile='native_1u'\n", CFG_ONU_MIB_PROFILE="native_1u")
        self.assert_valid(
            "schema_version=1\n[storage]\npersistent_root='preserve'\n", CFG_STORAGE_PERSISTENT_ROOT="preserve"
        )
        for section, key, value in (
            ("library_patch", "policy", "always"),
            ("onu", "mib_profile", "/etc/mibs/custom.ini"),
            ("storage", "persistent_root", "disabled"),
        ):
            with self.subTest(section=section):
                self.assert_invalid(f"schema_version=1\n[{section}]\n{key}='{value}'\n")

    def test_identity_validation_and_lengths(self) -> None:
        """Enforce identity formats and equipment-field byte limits."""
        for serial in ("ZTE12345678", "ZTEG123456789", "123412345678", "ZTEG1234567Z"):
            with self.subTest(serial=serial):
                self.assert_invalid(f"schema_version=1\n[onu]\nserial='{serial}'\n")
        for omcc in ("", "A0", "0xa", "0x100", "0xGG"):
            with self.subTest(omcc=omcc):
                self.assert_invalid(f"schema_version=1\n[onu]\nomcc_version='{omcc}'\n")
        for vendor in ("ABC", "ABCDE", "AB C"):
            with self.subTest(vendor=vendor):
                self.assert_invalid(f"schema_version=1\n[onu]\nvendor_id='{vendor}'\n")
        self.assert_valid("schema_version=1\n[onu]\nequipment_id='" + "E" * 20 + "'\n", CFG_ONU_EQUIPMENT_ID="E" * 20)
        self.assert_valid(
            "schema_version=1\n[onu]\nhardware_version='" + "H" * 14 + "'\n", CFG_ONU_HARDWARE_VERSION="H" * 14
        )
        self.assert_invalid("schema_version=1\n[onu]\nequipment_id='" + "E" * 21 + "'\n")
        self.assert_invalid("schema_version=1\n[onu]\nhardware_version='" + "H" * 15 + "'\n")

    def test_management_ipv4_validation(self) -> None:
        """Require unicast IPv4 addresses without prefixes or ambiguous octets."""
        for key in ("ip_address", "gateway", "ping_ip"):
            for value in (
                "256.1.2.3",
                "1.2.3",
                "1.2.3.4.5",
                "1..2.3",
                "01.2.3.4",
                "1.2.3.-1",
                "::1",
                "192.0.2.1/24",
                "0.0.0.0",  # ruff: ignore[hardcoded-bind-all-interfaces] - Invalid management-address fixture.
                "127.0.0.1",
                "224.0.0.1",
            ):
                with self.subTest(key=key, value=value):
                    self.assert_invalid(f"schema_version=1\n[management]\n{key}='{value}'\n")
        self.assert_valid("schema_version=1\n[management]\nping_ip='192.0.2.53'\n", CFG_MANAGEMENT_PING_IP="192.0.2.53")

    def test_management_netmask_validation(self) -> None:
        """Accept contiguous nonzero masks and reject malformed masks."""
        for mask in ("128.0.0.0", "255.0.0.0", "255.255.254.0", "255.255.255.254", "255.255.255.255"):
            with self.subTest(mask=mask):
                self.assert_valid(f"schema_version=1\n[management]\nnetmask='{mask}'\n", CFG_MANAGEMENT_NETMASK=mask)
        for mask in (
            "0.0.0.0",  # ruff: ignore[hardcoded-bind-all-interfaces] - Invalid netmask fixture.
            "255.0.255.0",
            "255.255.253.0",
            "255.254.128.0",
            "256.255.255.0",
            "24",
            "/24",
            "ffff:ffff::",
        ):
            with self.subTest(mask=mask):
                self.assert_invalid(f"schema_version=1\n[management]\nnetmask='{mask}'\n")

    def test_missing_file_and_directory_are_rejected(self) -> None:
        """Reject missing paths and directories as configuration sources."""
        for path in (self.root / "missing.toml", self.root):
            with self.subTest(path=path):
                result, values = self.load(path=path)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual({}, values)
                self.assertIn(b"missing or unreadable", result.stderr)

    def test_oversized_input_is_rejected(self) -> None:
        """Reject oversized configuration before parsing its contents."""
        self.assert_invalid("schema_version=1\n#" + "x" * 65536 + "\n")

    def test_shell_syntax_of_parser(self) -> None:
        """Verify the parser is valid BusyBox ash syntax."""
        assert BUSYBOX is not None
        result = subprocess.run(
            [BUSYBOX, "ash", "-n", str(SOURCE / "dei-config.sh")],
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
