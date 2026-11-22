"""Exercise the UniFi boot helper against an offline gateway."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from paths import ROOT

SHOW = "ip -o -4 addr show dev eth9"
ADD = "ip addr add dev eth9 local 192.168.11.2/24"
RULE = "POSTROUTING -o eth9 -d 192.168.11.0/24 -j SNAT --to-source 192.168.11.2"
CHECK = f"iptables -w 5 -t nat -C {RULE}"
APPEND = f"iptables -w 5 -t nat -A {RULE}"

MOCK = """#!/bin/sh
command=${0##*/}
printf '%s %s\\n' "$command" "$*" >> calls
case "$command $*" in
    'ip -o '*)
        [ ! -f missing-interface ] || exit 1
        echo '9: eth9 inet 192.168.11.20/24 scope global eth9'
        if [ -f address ]; then echo '9: eth9 inet 192.168.11.2/24 scope global eth9'; fi ;;
    'ip addr add '*) : > address ;;
    'iptables '*'-C '*)
        [ ! -f locked ] || exit 4
        [ -f rule ] ;;
    'iptables '*'-A '*) : > rule ;;
    *) exit 2 ;;
esac
"""


class UniFiOntAccessTests(unittest.TestCase):
    """Check interface and firewall mutations across boot and retry."""

    def setUp(self) -> None:
        """Expose only the mock network commands and the address parser."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ip = self.root / "ip"
        ip.write_text(MOCK)
        ip.chmod(0o700)
        (self.root / "iptables").symlink_to(ip)
        awk = shutil.which("awk")
        assert awk is not None
        (self.root / "awk").symlink_to(awk)

    def invoke(self) -> tuple[int, list[str]]:
        """Run the helper as systemd would with the isolated command path.

        Returns:
            The service exit status and the recorded network commands.

        """
        calls = self.root / "calls"
        calls.write_text("")
        result = subprocess.run(
            ["/bin/sh", ROOT / "tools/unifi-ont-access.sh"],
            cwd=self.root,
            env={"PATH": str(self.root)},
            check=False,
            timeout=5,
        )
        return result.returncode, calls.read_text().splitlines()

    def test_install_and_repeat(self) -> None:
        """Apply exactly once, accepting an existing address and matching rule."""
        self.assertEqual(self.invoke(), (0, [SHOW, ADD, CHECK, APPEND]))
        self.assertEqual(self.invoke(), (0, [SHOW, CHECK]))

    def test_missing_interface(self) -> None:
        """Leave the firewall untouched while waiting for the SFP+ interface."""
        (self.root / "missing-interface").touch()
        status, calls = self.invoke()
        self.assertNotEqual(status, 0)
        self.assertEqual(calls, [SHOW])

    def test_firewall_check_failure(self) -> None:
        """Stop before appending when xtables cannot acquire its lock."""
        (self.root / "locked").touch()
        self.assertEqual(self.invoke(), (4, [SHOW, ADD, CHECK]))
        (self.root / "locked").unlink()
        self.assertEqual(self.invoke(), (0, [SHOW, CHECK, APPEND]))
