"""Verify immutable fixture bytes and historical installer compatibility."""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Verify fixture inventory and accepted historical runtime hashes.

    Returns:
        Zero for valid fixtures, or one for integrity failures.

    """
    fixtures = ROOT / "tests/fixtures"
    checksums: dict[str, str] = json.loads((fixtures / "checksums.json").read_text())
    compatibility: dict[str, str | list[str]] = json.loads(
        (ROOT / "compat/previous-runtime-hashes.json").read_text(),
    )
    actual = {
        file.relative_to(fixtures).as_posix()
        for file in fixtures.rglob("*")
        if file.is_file() and file.name not in {"README.md", "checksums.json"}
    }
    errors = []
    if actual != checksums.keys():
        errors.append("Fixture inventory differs from tests/fixtures/checksums.json")
    for name, expected in checksums.items():
        file = fixtures / name
        if not file.is_file() or not file.resolve().is_relative_to(fixtures.resolve()):
            errors.append(f"Missing or invalid fixture: {name}")
            continue
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if digest != expected:
            errors.append(f"Historical fixture changed: {name}")
        if Path(name).parts[0].startswith("v"):
            accepted = compatibility.get(file.name, [])
            accepted = [accepted] if isinstance(accepted, str) else accepted
            if digest not in accepted:
                errors.append(f"Fixture is missing from accepted runtime hashes: {name}")
    for message in errors:
        print(message, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
