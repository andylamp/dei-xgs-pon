"""Checkout-relative locations shared by the offline test suite."""

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
DIST = ROOT / "dist"
CONFIG = ROOT / "config"
FIXTURES = ROOT / "tests" / "fixtures"
BUSYBOX = shutil.which(os.environ.get("BUSYBOX", "busybox")) or ""
