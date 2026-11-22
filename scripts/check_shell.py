"""Run syntax/static checks without executing module scripts."""

import argparse
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs trusted syntax/static-analysis tools only.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Check shell syntax and static analysis without running module code.

    Returns:
        Zero on success, or the failing tool's exit code; two when tools are missing.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated", action="store_true", help="Also validate the built installer")
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args()
    sources = args.files or sorted(path for directory in ("src", "tools") for path in (ROOT / directory).glob("*.sh"))
    if args.generated:
        sources.append(ROOT / "dist/dei-pon-install.sh")
    busybox = shutil.which(os.environ.get("BUSYBOX", "busybox"))
    shellcheck = shutil.which("shellcheck")
    if not busybox or not shellcheck:
        print("Install BusyBox and run uv sync --locked before checking shell scripts.", file=sys.stderr)
        return 2
    for source in sources:
        # ash -n parses only; no target commands are executed.
        result = subprocess.run([busybox, "ash", "-n", str(source)], cwd=ROOT, check=False)  # ruff: ignore[subprocess-without-shell-equals-true]
        if result.returncode:
            return result.returncode
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [shellcheck, "--rcfile", str(ROOT / ".shellcheckrc"), "-x", "-P", str(ROOT / "src"), *map(str, sources)],
        cwd=ROOT,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
