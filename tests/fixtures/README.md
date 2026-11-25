# Compatibility test fixtures

These files support offline compatibility and upgrade tests. They are not
configuration for a real ONU, and the tests do not contact network devices.

| Directory | Contents | Purpose |
| --- | --- | --- |
| `guide/` | Archived synthetic stand-ins and historical MD5 references | Verify historical bytes and guard against bundling old guide fingerprints |
| `v1/` | Exact runtime payloads from this project's first installer artifact | Test upgrades from the earlier generated runtime |
| `v0.1.0/` | Exact runtime payloads distributed before development-tool formatting | Preserve upgrades from the repository's initial release |
| `v0.1.0-toolchain/` | Exact runtime payloads after the toolchain formatting pass | Preserve upgrades when diagnostics and explicit patch opt-in are added |
| `v0.1.0-diagnostics/` | Exact runtime payloads from commit `725de37`, before the safety audit | Preserve upgrades when service verification and diagnostic safeguards are tightened |
| `v0.1.0-audit/` | Exact runtime payloads at commit `f83d6da` | Preserve upgrades from the audited runtime before enabling all ShellCheck rules |
| `v0.1.0-vanilla/` | Runtime from the vanilla installer before the VLAN-neutral configuration selector | Verify upgrades from installations using the earlier hook |
| `v0.1.0-selector/` | Exact runtime payloads at commit `0661fc6`, before optional management-port recovery | Preserve upgrades when the sixth, opt-in runtime file is added |

The files in `guide/` are synthetic. They do **not** contain the original guide's
scripts and are never executed. `guide/hashes.json` records the original-script
MD5 values published in Lefteri's
[DEI Fiber 8311 guide](https://rg.spes.gr/dei-fiber-8311/), updated 8 October 2026.
The guide informed the original troubleshooting procedure. Normal setup tests
start from vanilla firmware. The standalone removal tool uses SHA-256 fingerprints
of the active files observed on the guide-configured stick.

The normal installer accepts runtime hashes of releases of this project.
The binary-patch tests use a checksum adapter for synthetic library fixtures.
The production installer uses the device's real checksum commands.
These tests do not establish the contents of a vendor binary or replace testing
on the target firmware.

The files in `v1/` were extracted from the earlier generated
`dei835-install-v1.sh` artifact. The `v0.1.0/` files preserve the runtime distributed
before repository-wide formatting was introduced. The SHA-256 values accepted
for upgrades are recorded in
[the compatibility manifest](../../compat/previous-runtime-hashes.json).

These files preserve exact upgrade inputs and should not be edited as current
source; current runtime code belongs in `src/`. Formatting hooks exclude the
historical fixture bytes. A separate integrity check verifies all fixture files
against [their recorded SHA-256 values](checksums.json); this provenance README
is excluded from that manifest.

No vendor firmware or `libponnet` binary is included. Binary-patch tests generate
synthetic bytes and mock only the expected checksum recognition. This README
records provenance and does not assign a project license or license upstream
material.
