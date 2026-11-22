#!/usr/bin/env python3
"""Exercise bounded, read-only module observations with synthetic PON samples."""

import fcntl
import json
import unittest

import test_installer as support
from paths import BUSYBOX

PON_MOCK = """#!/usr/bin/env python3
import json
import os
from pathlib import Path

# Runtime validation has its own tests and needs a stable readiness response.
# Only the installer assessment and timed observation consume this sequence.
if os.environ.get("DEI_PON_CONF_DIR", os.environ.get("DEI835_CONF_DIR")) != os.environ["DEI_TEST_MAIN_CONF"]:
    print("errorcode=0 current=51 previous=40 time_curr=100")
    raise SystemExit(0)
source = Path(os.environ["DEI_TEST_PON_SAMPLES"])
data = json.loads(source.read_text())
counter = source.with_suffix(".counter")
index = int(counter.read_text()) if counter.exists() else 0
record = data[min(index, len(data) - 1)]
counter.write_text(str(index + 1))
module = Path(os.environ["DEI835_MODULE_ROOT"])
(module / "proc/uptime").write_text(str(record["uptime"]) + ".00 0.00\\n")
print(record["reply"])
"""


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class ModuleDiagnosticsTests(unittest.TestCase):
    """Check observations without invoking any device or persistent mutation."""

    def setUp(self) -> None:
        """Create a synthetic module and isolated executable PON sampler."""
        self.helper = support.InstallerTests("test_check_does_not_install_or_modify_configuration")
        self.helper.setUp()
        (self.helper.module / "proc").mkdir()
        program = self.helper.module / "usr/bin/pon"
        program.write_text(PON_MOCK)
        program.chmod(0o755)
        self.samples = self.helper.fixture.root / "pon-samples.json"
        self.helper.env["DEI_TEST_PON_SAMPLES"] = str(self.samples)
        self.helper.env["DEI_TEST_MAIN_CONF"] = str(self.helper.conf)

    def tearDown(self) -> None:
        """Remove the test module after each independent observation."""
        self.helper.tearDown()

    def configure_pon(self, replies: list[str], *, uptimes: list[int] | None = None) -> None:
        """Save samples with one extra first record for the library assessment."""
        records = [
            {"reply": reply, "uptime": uptimes[index] if uptimes else 1000 + index * 20}
            for index, reply in enumerate(replies)
        ]
        self.samples.write_text(json.dumps([records[0], *records]))
        self.samples.with_suffix(".counter").unlink(missing_ok=True)

    @staticmethod
    def sample(age: int, *, current: int = 51) -> str:
        """Build a complete successful vendor response.

        Returns:
            A synthetic state record without hardware identifiers.

        """
        return f"errorcode=0 current={current} previous=40 time_curr={age}"

    def assert_read_only(self) -> None:
        """Verify no setup, runtime, library, or installation audit was saved."""
        self.helper.assert_runtime_absent()
        for name in ("install.log", "dei-pon.toml", "setup-state", "dei835-originals", "dei-835-lib.sh"):
            self.assertFalse((self.helper.conf / name).exists(), name)
        self.assertEqual([], self.helper.fixture.state["mutations"])

    def test_default_window_observes_six_advancing_samples_without_setup_identity(self) -> None:
        """Allow read-only diagnostics before a fresh installation has a serial."""
        self.helper.fixture.state["fwenv"] = {}
        self.helper.fixture.state["fix_mode"] = ""
        self.configure_pon([self.sample(100 + index * 20) for index in range(6)])
        result = self.helper.invoke("diagnose")
        self.helper.success(result)
        self.assertEqual(6, result.stdout.count("Sample "))
        self.assertIn("6 samples, 20s between samples", result.stdout)
        self.assertIn("O5 with an advancing state age", result.stdout)
        self.assertIn("8311_fix_vlans=unset; 8311_omcc_version=unset", result.stdout)
        self.assert_read_only()

    def test_timer_reset_is_reported_without_claiming_an_olt_fault(self) -> None:
        """Detect reactivation between O5 samples with increasing module uptime."""
        self.configure_pon([self.sample(300), self.sample(2)])
        result = self.helper.invoke("diagnose", extra=("--samples", "2", "--interval", "1"))
        self.helper.success(result)
        self.assertIn("timer-resets=1 module-restarts=0", result.stdout)
        self.assertIn("do not establish an OLT fault", result.stdout)
        self.assertNotIn("O5 with an advancing state age", result.stdout)
        self.assert_read_only()

    def test_decreasing_uptime_identifies_module_restart(self) -> None:
        """Separate a restart observation from the state timer alone."""
        self.configure_pon([self.sample(300), self.sample(2)], uptimes=[1000, 5])
        result = self.helper.invoke("diagnose", extra=("--samples", "2", "--interval", "1"))
        self.helper.success(result)
        self.assertIn("timer-resets=1 module-restarts=1", result.stdout)

    def test_missing_duplicate_and_stale_samples_cannot_establish_stability(self) -> None:
        """Keep partial, ambiguous and nonadvancing state observations inconclusive."""
        scenarios = (
            [self.sample(10), "errorcode=1"],
            [self.sample(10), self.sample(20) + "\n" + self.sample(1)],
            [self.sample(10), self.sample(10)],
            [self.sample(10, current=40), self.sample(20, current=40)],
        )
        for replies in scenarios:
            with self.subTest(replies=replies):
                self.configure_pon(replies)
                result = self.helper.invoke("diagnose", extra=("--samples", "2", "--interval", "1"))
                self.helper.success(result)
                self.assertIn("Stable O5 is not established", result.stdout)
                self.assertNotIn("O5 with an advancing state age", result.stdout)

    def test_state_transition_is_reported(self) -> None:
        """Record leaving or entering O5 as a continuity interruption."""
        self.configure_pon([self.sample(100), self.sample(1, current=40)])
        result = self.helper.invoke("diagnose", extra=("--samples", "2", "--interval", "1"))
        self.helper.success(result)
        self.assertIn("non-O5=1 state-changes=1", result.stdout)
        self.assertIn("Continuity was interrupted", result.stdout)

    def test_diagnose_does_not_acquire_firmware_hook_lock(self) -> None:
        """Avoid blocking provisioning while the observation window is running."""
        self.configure_pon([self.sample(100)])
        with (self.helper.fixture.root / "firmware.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.helper.invoke("diagnose", extra=("--samples", "1"))
        self.helper.success(result)
        self.assertIn("no firmware hook lock held", result.stdout)
        self.assert_read_only()

    def test_observation_options_are_bounded_and_diagnose_only(self) -> None:
        """Reject invalid windows before commands or setup side effects."""
        cases = (
            ("check", ("--samples", "2")),
            ("install", ("--interval", "2")),
            ("diagnose", ("--samples", "0")),
            ("diagnose", ("--samples", "31")),
            ("diagnose", ("--interval", "0")),
            ("diagnose", ("--interval", "61")),
            ("diagnose", ("--samples", "2", "--samples", "3")),
            ("diagnose", ("--samples",)),
        )
        for action, extra in cases:
            with self.subTest(action=action, extra=extra):
                self.assertEqual(2, self.helper.invoke(action, extra=extra).returncode)
        self.assert_read_only()

    def test_failed_interval_stops_observation_without_success_summary(self) -> None:
        """Do not claim a completed window when the requested delay fails."""
        self.configure_pon([self.sample(100), self.sample(120)])
        self.helper.proxies += "\nsleep() { return 23; }\n"
        result = self.helper.invoke("diagnose", extra=("--samples", "2", "--interval", "1"))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("requested sample interval could not be completed", result.stderr)
        self.assertNotIn("PON summary:", result.stdout)
        self.assert_read_only()

    def test_inventory_omits_secrets_and_does_not_follow_symlinks(self) -> None:
        """Report safe metadata rather than replaying arbitrary stored values."""
        self.configure_pon([self.sample(100)])
        self.helper.fixture.state["fwenv"]["8311_omcc_version"] = "PRIVATE_VALUE\ninjected"
        (self.helper.conf / "private-key").write_text("PRIVATE_VALUE")
        (self.helper.conf / "dei-pon.toml").symlink_to(self.helper.conf / "private-key")
        result = self.helper.invoke("diagnose", extra=("--samples", "1"))
        self.helper.success(result)
        self.assertIn("8311_omcc_version=unrecognized", result.stdout)
        self.assertIn("dei-pon.toml: symlink (not followed)", result.stdout)
        self.assertNotIn("PRIVATE_VALUE", result.stdout + result.stderr)
        self.assertNotIn("private-key", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
