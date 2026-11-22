"""Exercise firmware hook configuration handoff to the real watcher under BusyBox."""

import json
import shutil
import subprocess
import time
import unittest

import test_installer as support
from paths import BUSYBOX, SOURCE


@unittest.skipUnless(BUSYBOX, "BusyBox is required to exercise the target shell")
class HookConfigTests(unittest.TestCase):
    """Run the shipped hook and watcher in an isolated module filesystem."""

    def setUp(self) -> None:
        """Stage the project runtime and restrict shell commands to synthetic module state."""
        self.helper = support.InstallerTests("test_check_does_not_install_or_modify_configuration")
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.module = self.helper.fixture
        for name in support.RUNTIME:
            shutil.copyfile(SOURCE / name, self.module.conf / name)
        self.done = self.module.root / "watcher-exit"
        self.module.run_dir.mkdir()
        self.saved = self.module.conf / "dei-pon.toml"
        self.log = self.module.conf / "dei835.log"
        self.env = {
            **self.helper.env,
            "DEI_PON_CONF_DIR": str(self.module.conf),
            "DEI_PON_RUN_DIR": str(self.module.run_dir),
            "DEI835_MAX_PASSES": "1",
            "DEI_TEST_HOOK_EXIT": str(self.done),
        }
        self.env.pop("DEI_PON_CONFIG_PATH", None)

    def run_hook(self, *, expect_rejection: bool = False) -> str:
        """Allow the firmware entry point to launch one complete, read-only watcher pass.

        Returns:
            The watcher's runtime log or the reason the hook refused to launch it.

        """
        self.done.unlink(missing_ok=True)
        self.log.unlink(missing_ok=True)
        self.module.state_path.write_text(json.dumps(self.module.state))
        (self.module.run_dir / "stop").touch()
        # Capture when the detached watcher finishes. Refuse any path outside
        # the fixture before starting a worker or removing its stop marker.
        shell = (
            self.helper.proxies
            + """
rm() { [ "$#" = 2 ] && [ "$1" = -f ] && [ "$2" = "$DEI_PON_RUN_DIR/stop" ] && command rm "$@"; }
sh() {
    if [ "$1" = "$DEI_PON_CONF_DIR/keyring.sh" ]; then "$DEI835_MOCK_BIN/sh" "$@"; else return 1; fi
    printf '%s\\n' "$?" >"$DEI_TEST_HOOK_EXIT.tmp" && mv "$DEI_TEST_HOOK_EXIT.tmp" "$DEI_TEST_HOOK_EXIT"
}
. "$1"
"""
        )
        result = subprocess.run(
            [self.helper.busybox, "ash", "-c", shell, "offline-hook", str(self.module.conf / "vlan_fixes_hook.sh")],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if expect_rejection:
            self.assertNotEqual(0, result.returncode)
            self.assertTrue((self.module.run_dir / "stop").is_file())
            self.assertFalse(self.done.exists())
            self.assertFalse(self.log.exists())
            return result.stderr
        self.helper.success(result)
        deadline = time.monotonic() + 10
        while not self.done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.done.is_file(), "firmware hook did not complete its isolated watcher pass")
        self.assertEqual("0", self.done.read_text().strip())
        observed = json.loads(self.module.state_path.read_text())
        self.assertFalse(observed.get("mock_errors"))
        self.assertEqual([], observed["mutations"])
        return self.log.read_text()

    def test_saved_toml_selects_each_services_mapper_from_the_same_hook(self) -> None:
        """Select the saved VLAN on a fresh firmware invocation for either supported service."""
        for vlan, mapper in ((835, "pmapper1"), (836, "pmapper2")):
            with self.subTest(vlan=vlan):
                self.module.configure(vlan=vlan)
                self.assertIn(f"Waiting for stable VLAN {vlan}: {mapper}", self.run_hook())

    def test_explicit_config_wins_over_saved_config_and_older_environment(self) -> None:
        """Hand the operator's explicit path, including spaces, unchanged to the watcher."""
        candidate = self.module.root / "another service.toml"
        candidate.write_text(self.module.configure(vlan=836))
        self.module.configure(vlan=835)
        self.env["DEI835_CONFIG_PATH"] = str(self.saved)
        self.env["DEI_PON_CONFIG_PATH"] = str(candidate)
        self.assertIn("Waiting for stable VLAN 836: pmapper2", self.run_hook())

    def test_missing_hook_selection_never_triggers_legacy_default_vlan(self) -> None:
        """Hold the watcher when its saved TOML or its explicit selection cannot be used."""
        self.saved.unlink(missing_ok=True)
        self.assertIn("selected TOML is missing or unreadable", self.run_hook(expect_rejection=True))
        self.module.configure(vlan=835)
        self.env["DEI835_CONFIG_PATH"] = str(self.saved)
        for path in ("", str(self.module.root / "missing.toml")):
            with self.subTest(path=path):
                self.env["DEI_PON_CONFIG_PATH"] = path
                report = self.run_hook(expect_rejection=True)
                self.assertIn("selected TOML is missing or unreadable", report)
