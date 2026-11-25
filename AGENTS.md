# AGENTS

## Project and ownership

This repository builds a configuration-driven BusyBox installer for DEI Fiber
XGS-PON modules running 8311 community firmware. The module runtime is shell;
Python builds artifacts and runs offline tests. Device protocol implementations
and vendor firmware belong to their upstream projects.

- `src/`: configuration parser, runtime service discovery and repair, phased
  setup, guarded library operations, and the opt-in management-port helper.
- `tools/remove-guide-scripts.sh`: standalone, one-time removal of the known
  external guide scripts. Keep their fingerprints and removal logic outside the
  installer and its release archive.
- `config/dei-pon.example.toml`: the tracked, credential-free configuration
  example and documented defaults.
- `scripts/build_installer.py`: generated installer, archive, and checksums.
- `tests/`: synthetic module state and mocked commands; no real device access.
- `compat/`: recognized historical hashes and compatibility material.
- `dist/`: generated, ignored build output.
- `local/`: ignored private configuration and local notes.
- `README.md`: operator instructions, validation status, and recovery limits.

Use descriptive `feat/`, `bugfix/`, or `chore/` branches and preserve the default
branch. Do not add Codex co-author attribution to commit messages. Preserve
existing attribution for other contributors.

## Configuration and private data

One TOML file controls the installation. Keep real configurations in ignored
`local/` or at ignored `config/dei-pon.toml`. Never commit an actual ONU serial,
credentials, or private captures. Use synthetic identities in tests.

Parse configuration as data. Never source it, evaluate it as shell code, or
silently accept unsupported TOML constructs. Keep the strict parser, template,
installer behavior, and documentation consistent. Blank identity and management
fields preserve current values; a fresh installation must explicitly provide
its serial and MIB profile.

Checks must not print sensitive configuration values. Report planned firmware
variable names without their values. Store saved TOML and original-setting
backups privately, preserving whether each original variable was absent or empty.
Normal installation starts with empty runtime paths or a verified release of
this project. Reject other active scripts before changing setup. Runtime rollback
removes verified project scripts. Do not reinstall or back up external guide
scripts, overwrite unknown backup files, or remove locally edited scripts.

## Runtime and maintenance invariants

Discover the selected VLAN's mapper and GEMs from current configuration. Do not
reintroduce fixed GEM ranges or installation-specific bridge-port writes.
Support only verified rule patterns; ambiguous discovery must stop changes.
Keep OMCI instance-ID assumptions and hardware-validation limits explicit.
Report all observed numeric GEM interfaces separately from the verified selected
mapper members. Print selected IDs, roles, driver properties, current encryption,
and configured targets; never present the inventory as an apply target. Keep
check modes stdout-only with no persistent audit writes. Installation audits must
be private, bounded, and sanitized; do not copy raw configuration or OMCI logs.
Log complete stable/new mappings and changed failures without repeating identical
discovery snapshots on every poll.

Management-port (LCT) recovery is the only hardware bridge-port write. It
requires explicit `[management_recovery] enabled = true`, stays disabled by
default, and lives in `src/dei-lct.sh`, which the runtime loads only after that
opt-in. Discover the port from the switch table and the LCT interface name;
never hard-code its number or accept one from configuration. Write only the
recorded detached preimages, after matching consecutive samples and either a
verified healthy service or the unchanged fallback window, then verify the
readback. Bound every `switch_cli` call, keep check and status read-only, and
never write another bridge port. Keep its hardware-validation limits explicit.

Repair only the selected service. Preserve native passthrough rules and unrelated
services. Multi-rule normalization must recover safely from partial application.
Treat key-error counters as cumulative and verify changes using counter deltas.

Coordinate the firmware hook, installer, and watcher with their existing locks.
Keep one watcher active across upgrades. Defer runtime writes until provisioning
is stable and refuse them while firmware failsafe exists. Never kill or apply a
timeout to `omci_pipe.sh`; it owns firmware locks that can otherwise remain held.

Never restart `omcid`, reboot automatically, or modify a live library mapping in
place. Setup must report required maintenance phases and leave the reboot to the
operator. Verify persistent backing and any required across-boot marker before
claiming a patch is durable. Preserve user-owned failsafe files; remove only an
installer-owned marker whose ownership still matches.

Binary patching requires explicit `[library_patch] enabled = true` as well as a
permitting policy. Keep it disabled by default and never infer opt-in from a log
message or O5. Distinguish UNI compatibility signatures from GEM decryption
failures; log evidence is advisory and does not bypass other guards.

Binary patching requires the exact supported preimage, applicable UNI layout,
verified backup, safe maintenance state, single-byte verification, and expected
resulting hash. Unknown firmware or library contents must never authorize a
patch. Keep the original guide and compatibility fixture checksums unchanged.
Use atomic file replacement and retain recoverable originals.

Runtime rollback is not a full setup uninstall. Keep its boundary explicit:
identity, boot settings, MIBs, saved configuration, and library state require
their own handling. Do not silently restore a stock library under an incompatible
UNI-0x0205 MIB or claim that rollback reverses live settings.

## Development checks

Use the pinned Python 3.15.2, `uv` 0.13.7 or newer, the checked-in lockfile, and
host BusyBox. `make dev` synchronizes the locked development environment and
installs the local `prek` commit hook. Linters and formatters come from the locked
`uv` environment, including ShellCheck; the module runtime remains BusyBox shell.
After changes, run:

```sh
make check
```

Use `make format`, `make build`, `make test`, and `make lint` for focused work.
Tests use at most four workers by default; `make test-serial` runs without parallel
workers. Pass `TEST_ARGS="tests/test_config.py"` to focus a test run. The commit
hook performs fast file and code checks; full tests and
reproducible-build checks run through `make test` and CI.

Ruff and ty check Python; ShellCheck and shfmt check the module shell; PyMarkdown
checks Markdown; actionlint checks GitHub workflows. Keep their configuration in
step with the checked-in dependencies. When changing Python versions, keep
`.python-version`, the project requirement, and the Ruff/ty targets consistent.
Use locked `uv` commands rather than unversioned global development tools.

Ruff selects all rules, including preview rules. Fix new findings rather than
disabling whole rule families. Keep any necessary exception narrowly scoped and
document its reason; preserve the existing unittest harness and immutable data.

ShellCheck enables all optional rules for POSIX shell in `.shellcheckrc`.
Use the same policy for the source scripts and generated installer. Document
any false positive at the affected statement.

Do not edit or commit generated `dist/` files; update their source and rebuild.
Never autoformat historical files under `tests/fixtures/`. Formatting hooks
exclude those bytes, and an integrity check verifies their recorded hashes.
Retain recognition of previously distributed runtime payloads when current
source formatting changes their hashes. Add meaningful offline regression
coverage for changed parser behavior, discovery guards, partial failures, setup
phases, ownership, or binary operations.

Do not contact, probe, reboot, or reconfigure a real ONT or router as part of
development validation. Use synthetic filesystem roots and mocked commands.
Do not access an existing deployment or its private configuration to make tests
pass. Clearly distinguish offline test results from actual hardware validation.

Preserve the upstream guide and firmware-project credits. Do not invent a license
or assign licensing terms to third-party material.
