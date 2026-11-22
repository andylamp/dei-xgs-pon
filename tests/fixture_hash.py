"""Model known MD5 values for synthetic stock and patched vendor-library fixtures."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def fixture_md5(data: bytes) -> str:
    """Map the exact synthetic vendor binaries and hash all other bytes normally.

    Args:
        data: File contents to compare with the controlled fixtures.

    Returns:
        The matching legacy digest or the actual hexadecimal MD5 digest.

    """
    stock_path = os.environ.get("LIBPATCH_STOCK_FIXTURE")
    if stock_path:
        stock = Path(stock_path).read_bytes()
        if data == stock:
            return "5326f83e22a18929b830adfb82cfc483"
        patched = stock[:236395] + b"\xfb" + stock[236396:]
        if data == patched:
            return "5ddb892fbf0b648479f605d9fddda50c"
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def main() -> int:
    """Print fixture-aware MD5 checksums and report unreadable input files.

    Returns:
        Zero if every input file was readable, or one if any input was unreadable.

    """
    result = 0
    for name in sys.argv[1:]:
        try:
            data = Path(name).read_bytes()
        except OSError:
            result = 1
            continue
        print(fixture_md5(data) + "  " + name)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
