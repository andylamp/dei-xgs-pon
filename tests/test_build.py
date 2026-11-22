"""Validate the distributable independently from the installer simulations."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from paths import CONFIG, DIST, FIXTURES, ROOT, SOURCE


class BuildTests(unittest.TestCase):
    """Check that generated downloads are complete and reproducible."""

    def test_installer_contains_no_external_guide_cleanup_or_fingerprints(self) -> None:
        """Keep recognition and removal of third-party scripts outside normal operations."""
        installer = (DIST / "dei-pon-install.sh").read_text()
        old_md5 = json.loads((FIXTURES / "guide/hashes.json").read_text())
        cleanup = (ROOT / "tools/remove-guide-scripts.sh").read_text()
        guide_sha256 = re.findall(r"\b[0-9a-f]{64}\b", cleanup)
        self.assertTrue(guide_sha256)
        for digest in (*old_md5.values(), *guide_sha256):
            self.assertNotIn(digest, installer)
        self.assertNotIn(cleanup, installer)

    def test_installer_embeds_current_sources_with_matching_checksums(self) -> None:
        """Verify embedded payloads match current sources and recorded checksums."""
        installer = (DIST / "dei-pon-install.sh").read_text(encoding="utf-8")
        payloads = dict(
            re.findall(
                r"cat > \"\$\{S\}/([^\"]+)\" <<'DEI_PAYLOAD_END'\n(.*?)\nDEI_PAYLOAD_END",
                installer,
                re.DOTALL,
            )
        )
        expected = {source.name for source in SOURCE.glob("*.sh")}
        self.assertIn((SOURCE / "checksums.sh").read_text(), installer)
        self.assertEqual(expected, {*payloads, "checksums.sh"})
        for name, text in payloads.items():
            payload = (text + "\n").encode("utf-8")
            self.assertEqual((SOURCE / name).read_text(encoding="utf-8").encode("utf-8"), payload)
            digest = hashlib.sha256(payload).hexdigest()
            self.assertIn(f"{name}) echo {digest};;", installer)
        self.assertNotIn("/tmp/dei-835-persistence", installer)  # ruff: ignore[hardcoded-temp-file] - Reject leaked development paths.
        self.assertNotRegex(installer, r"/home/[^/\s]+")

    def test_bundle_is_minimal_and_checksums_cover_all_downloads(self) -> None:
        """Verify the archive contains only public files with complete checksums."""
        with zipfile.ZipFile(DIST / "dei-pon-setup.zip") as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(["dei-pon-install.sh", "dei-pon.toml"], archive.namelist())
            for name in archive.namelist():
                self.assertEqual((DIST / name).read_bytes(), archive.read(name))
                self.assertEqual((1980, 1, 1, 0, 0, 0), archive.getinfo(name).date_time)
        config = tomllib.loads((DIST / "dei-pon.toml").read_text())
        self.assertEqual("", config["onu"]["serial"])
        self.assertEqual((CONFIG / "dei-pon.example.toml").read_bytes(), (DIST / "dei-pon.toml").read_bytes())
        hashes = {}
        for line in (DIST / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split()
            hashes[name] = digest
            self.assertEqual(hashlib.sha256((DIST / name).read_bytes()).hexdigest(), digest)
        self.assertEqual({"dei-pon-install.sh", "dei-pon.toml", "dei-pon-setup.zip"}, set(hashes))

    def test_build_is_reproducible_outside_checkout_with_normalized_line_endings(self) -> None:
        """Verify build bytes ignore checkout location, timestamps, and CRLF input."""
        with tempfile.TemporaryDirectory(prefix="dei-build-test-") as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()
            for directory in ("src", "config", "compat"):
                shutil.copytree(ROOT / directory, checkout / directory)
            (checkout / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/build_installer.py", checkout / "scripts/build_installer.py")
            shutil.copyfile(ROOT / "pyproject.toml", checkout / "pyproject.toml")
            command = [sys.executable, str(checkout / "scripts/build_installer.py")]
            subprocess.run(command, cwd=temporary, check=True, capture_output=True, timeout=30)
            first = {artifact.name: artifact.read_bytes() for artifact in (checkout / "dist").iterdir()}
            for source in (checkout / "src").glob("*.sh"):
                source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
                os.utime(source, (123456789, 123456789))
            subprocess.run(command, cwd=checkout / "src", check=True, capture_output=True, timeout=30)
            second = {artifact.name: artifact.read_bytes() for artifact in (checkout / "dist").iterdir()}
            self.assertEqual(first, second)
            for name, payload in first.items():
                self.assertEqual((DIST / name).read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
