#!/usr/bin/env python3
"""Bundle the configuration-driven DEI setup tool for BusyBox modules."""

import hashlib
import json
import stat
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
OUTPUT = ROOT / "dist"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
FILES = ("dei-config.sh", "dei-835-lib.sh", "keyring.sh", "status.sh", "vlan_fixes_hook.sh")
PAYLOADS = (
    *FILES,
    "libponnet-patch.sh",
    "setup-engine.sh",
    "installation-log.sh",
    "module-diagnostics.sh",
    "gem-diagnostics.sh",
    "uninstall-runtime.sh",
    "uninstall-env.sh",
)
PREVIOUS = json.loads((ROOT / "compat/previous-runtime-hashes.json").read_text())
header = r"""#!/bin/sh
# DEI PON configuration-driven setup @PROJECT_VERSION@, for 8311 v2.8.3.
# Usage: sh dei-pon-install.sh ACTION [--config PATH] or uninstall [--dry-run]
# ACTION: check, diagnose, install, uninstall, rollback, patch-library, rollback-library
# check validates/plans; install uses an explicit/saved TOML configuration.
# No automatic reboot or omcid restart. A required maintenance reboot is reported.
set -u
D=${DEI_PON_CONF_DIR:-${DEI835_CONF_DIR:-/ptconf/8311}}
R=${DEI_PON_RUN_DIR:-${DEI835_RUN_DIR:-/tmp/dei835}}
FIRMWARE_LOCK=${DEI835_FIRMWARE_LOCK:-/tmp/8311-fix-vlans.lock}
CONFIG_BEFORE="${D}/setup-state/dei-pon.toml.before"
ACTION=check
CONFIG_INPUT=${DEI_PON_CONFIG_PATH-${DEI835_CONFIG_PATH:-}}
CONFIG_ENV_SET=${DEI_PON_CONFIG_PATH+x}${DEI835_CONFIG_PATH+x}
CONFIG_SEEN=0
UNINSTALL_PREVIEW=0
DIAG_SAMPLES=6
DIAG_INTERVAL=20
DIAG_SAMPLES_SEEN=0
DIAG_INTERVAL_SEEN=0
if [ "$#" -gt 0 ]; then
    case "$1" in check|diagnose|install|uninstall|rollback|patch-library|rollback-library) ACTION=$1; shift;; *) :;; esac
fi
while [ "$#" -gt 0 ]; do
    case "$1" in
        --config)
            [ "${ACTION}" != uninstall ] || { echo 'uninstall uses installation records; --config is not applicable' >&2; exit 2; }
            [ "$#" -ge 2 ] && [ -n "$2" ] || { echo "--config requires a nonempty path" >&2; exit 2; }
            [ "${CONFIG_SEEN}" = 0 ] || { echo "--config may only be supplied once" >&2; exit 2; }
            CONFIG_INPUT=$2; CONFIG_SEEN=1; shift 2;;
        --samples)
            [ "${ACTION}" = diagnose ] && [ "$#" -ge 2 ] && [ "${DIAG_SAMPLES_SEEN}" = 0 ] || { echo '--samples requires diagnose and one value' >&2; exit 2; }
            case "$2" in [1-9]|[12][0-9]|30) DIAG_SAMPLES=$2;; *) echo '--samples must be 1..30' >&2; exit 2;; esac
            DIAG_SAMPLES_SEEN=1; shift 2;;
        --interval)
            [ "${ACTION}" = diagnose ] && [ "$#" -ge 2 ] && [ "${DIAG_INTERVAL_SEEN}" = 0 ] || { echo '--interval requires diagnose and one value' >&2; exit 2; }
            case "$2" in [1-9]|[1-5][0-9]|60) DIAG_INTERVAL=$2;; *) echo '--interval must be 1..60 seconds' >&2; exit 2;; esac
            DIAG_INTERVAL_SEEN=1; shift 2;;
        --dry-run)
            [ "${ACTION}" = uninstall ] && [ "${UNINSTALL_PREVIEW}" = 0 ] || { echo '--dry-run is valid once for uninstall' >&2; exit 2; }
            UNINSTALL_PREVIEW=1; shift;;
        -h|--help) echo "Usage: $0 check|diagnose|install|rollback|patch-library|rollback-library [--config PATH]"; echo "       $0 uninstall [--dry-run]"; echo 'diagnose options: --samples 1..30 (default 6), --interval 1..60 (default 20 seconds)'; exit 0;;
        *) echo "Unknown argument: $1" >&2; exit 2;;
    esac
done
die() { echo "ERROR: $*" >&2; exit 1; }
install_cleanup() {
    install_exit_status=$1
    if [ "${INSTALL_AUDIT_ACTIVE:-0}" = 1 ] && ! install_audit_finish "${install_exit_status}"; then
        echo "ERROR: audit completion could not be saved; setup changes may already have been applied" >&2
        [ "${install_exit_status}" != 0 ] || install_exit_status=1
    fi
    rm -rf "${S}"
    trap - EXIT
    exit "${install_exit_status}"
}
if { [ "${ACTION}" = uninstall ] || [ "${ACTION}" = rollback ]; } && [ ! -e "${D}" ] && [ ! -L "${D}" ] && [ -d "${D%/*}" ]; then
    echo 'Runtime directory absent. No active scripts or firmware recovery records to process.'
    exit 0
fi
[ -d "${D}" ] && [ ! -L "${D}" ] || die "${D} must be a real directory; run this on the module"
for cmd in awk cat chmod cmp cp cut date flock grep head md5sum mktemp mv sed sha256sum sh stat tail tr wc; do
    command -v "${cmd}" >/dev/null 2>&1 || die "Required command missing: ${cmd}"
done
S=$(mktemp -d /tmp/dei-pon-install.XXXXXX) || die "Cannot create staging directory"
trap 'install_cleanup $?' EXIT
trap 'exit 1' INT TERM HUP
"""
payload_text = {name: (SOURCE / name).read_text(encoding="utf-8") for name in PAYLOADS}
parts = [header.replace("@PROJECT_VERSION@", VERSION), (SOURCE / "checksums.sh").read_text(encoding="utf-8")]
for name in PAYLOADS:
    parts.extend((f"cat > \"${{S}}/{name}\" <<'DEI_PAYLOAD_END'\n", payload_text[name], "DEI_PAYLOAD_END\n"))
parts.append('new_hash() {\n    case "$1" in\n')
for name, payload in payload_text.items():
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    parts.append(f"        {name}) echo {digest};;\n")
parts.extend(("        *) return 1;;\n    esac\n}\n", 'previous_hash() {\n    case "$1" in\n'))
for name, previous in PREVIOUS.items():
    digests = [previous] if isinstance(previous, str) else previous
    parts.append(f"        {name})\n")
    parts.extend(f"            echo {digest}\n" for digest in digests)
    parts.append("            ;;\n")
parts.extend((
    "        *) return 1;;\n    esac\n}\n",
    f'PAYLOADS="{" ".join(PAYLOADS)}"\n',
    r"""
FILES="dei-config.sh dei-835-lib.sh keyring.sh status.sh vlan_fixes_hook.sh"
for name in ${PAYLOADS}; do
    expected=$(new_hash "${name}") && actual=$(sha "${S}/${name}") && [ "${expected}" = "${actual}" ] || die "Payload checksum failed: ${name}"
    sh -n "${S}/${name}" || die "Payload syntax failed: ${name}"
    chmod 755 "${S}/${name}" || die "Cannot set staged permissions"
done
if [ "${ACTION}" != diagnose ]; then
    exec 8>"${FIRMWARE_LOCK}"
    flock -n 8 || die "Firmware hook is active; retry after it finishes"
fi
. "${S}/libponnet-patch.sh"
. "${S}/installation-log.sh"
. "${S}/module-diagnostics.sh"

# Optional stored files must be regular and never aliases to another path.
regular_or_absent() {
    [ ! -L "$1" ] && { [ ! -e "$1" ] || [ -f "$1" ]; }
}
# Only project release bytes may occupy a managed runtime path.
runtime_file_check() {
    regular_or_absent "${D}/$1" || { echo "ERROR: $1 is a symlink or special file" >&2; return 1; }
    if [ ! -e "${D}/$1" ]; then
        RUNTIME_FILE_STATE=absent
        return 0
    fi
    installed_digest=$(sha "${D}/$1") && bundled_digest=$(new_hash "$1") || return 1
    if [ "${installed_digest}" = "${bundled_digest}" ]; then
        RUNTIME_FILE_STATE=current
    elif previous_hash "$1" | grep -Fxq "${installed_digest}"; then
        RUNTIME_FILE_STATE=previous
    else
        echo "ERROR: $1 is not a recognized project runtime; refusing to overwrite or remove it" >&2
        return 1
    fi
}
runtime_files_check() {
    RUNTIME_PRESENT=0
    for runtime_name in ${FILES}; do
        runtime_file_check "${runtime_name}" || return 1
        if [ "${RUNTIME_FILE_STATE}" != absent ]; then RUNTIME_PRESENT=1; fi
    done
}
runtime_files_remove() {
    runtime_files_check || return 1
    # Stop event-driven startup first; the caller holds firmware and worker locks.
    for runtime_name in vlan_fixes_hook.sh keyring.sh status.sh dei-835-lib.sh dei-config.sh; do
        runtime_file_check "${runtime_name}" || return 1
        if [ "${RUNTIME_FILE_STATE}" != absent ]; then rm "${D}/${runtime_name}" || return 1; fi
    done
}
worker_paths_check() {
    if [ -L "${R}" ] || { [ -e "${R}" ] && [ ! -d "${R}" ]; }; then
        echo 'ERROR: Invalid worker directory' >&2
        return 1
    fi
    for worker_file in stop worker.lock; do
        regular_or_absent "${R}/${worker_file}" || { echo "ERROR: Invalid worker ${worker_file} path" >&2; return 1; }
    done
}
saved_paths_check() {
    regular_or_absent "${D}/dei-pon.toml" || { echo 'ERROR: Saved configuration is a symlink or special file' >&2; return 1; }
    if [ -L "${D}/setup-state" ] || { [ -e "${D}/setup-state" ] && [ ! -d "${D}/setup-state" ]; }; then
        echo 'ERROR: Invalid setup-state directory' >&2
        return 1
    fi
    regular_or_absent "${CONFIG_BEFORE}" || { echo 'ERROR: Invalid configuration backup path' >&2; return 1; }
}
# Stage on the destination filesystem for atomic replacement.
atomic_copy() {
    copy_source=$1
    copy_target=$2
    copy_mode=$3
    regular_or_absent "${copy_target}" || {
        echo "ERROR: refusing a symlink or special destination: ${copy_target}" >&2
        return 1
    }
    copy_stage=$(mktemp "${copy_target%/*}/.dei-pon-stage.XXXXXX") || return 1
    # Retain mktemp's private mode while copying configuration bytes.
    cp "${copy_source}" "${copy_stage}" || { rm -f "${copy_stage}"; return 1; }
    if ! cmp -s "${copy_source}" "${copy_stage}"; then
        rm -f "${copy_stage}"
        return 1
    fi
    if ! chmod "${copy_mode}" "${copy_stage}"; then
        rm -f "${copy_stage}"
        return 1
    fi
    if ! regular_or_absent "${copy_target}" || ! mv -f "${copy_stage}" "${copy_target}"; then
        rm -f "${copy_stage}"
        return 1
    fi
}

if [ "${ACTION}" = uninstall ]; then
    . "${S}/uninstall-runtime.sh" || die "Cannot load uninstall checks"
    . "${S}/dei-config.sh" || die "Cannot load configuration checks"
    . "${S}/setup-engine.sh" || die "Cannot load setup checks"
    . "${S}/uninstall-env.sh" || die "Cannot load firmware recovery checks"
    uninstall_runtime_check || die "Runtime removal preflight failed"
    uninstall_env_plan || die "Firmware recovery preflight failed"
    if [ "${UNINSTALL_PREVIEW}" = 1 ]; then
        for name in ${FILES}; do
            if [ -f "${D}/${name}" ]; then printf 'Would remove active file: %s\n' "${D}/${name}"; fi
        done
        if [ -d "${D}/setup-state" ]; then
            echo 'Would move the inactive installation records and saved configuration into a private recovery directory.'
        fi
        if [ "${UNINSTALL_RUNTIME_WORK}" = 0 ]; then echo 'No active installation to remove.'; fi
        echo 'Uninstall preview complete. No changes applied.'
        exit 0
    fi
    if [ "${UNINSTALL_RUNTIME_WORK}" = 0 ]; then
        echo 'No active installation to remove.'
        exit 0
    fi
    lp_lock_worker || die "Worker stop requested; retry uninstall after the current operation finishes"
    uninstall_runtime_check || die "Runtime files changed during preflight; review and retry"
    uninstall_env_apply || die "Firmware restoration incomplete; retained recovery records for retry"
    uninstall_runtime_remove || die "Runtime removal incomplete; review and retry"
    lp_unlock_worker
    echo 'Uninstall complete. No reboot performed. Live PON state lasts until reprovisioning or reboot.'
    exit 0
fi

if [ "${ACTION}" != diagnose ] && { [ -e "${D}/.dei-uninstall-archive" ] || [ -L "${D}/.dei-uninstall-archive" ]; }; then
    die "A previous uninstall has a pending recovery archive; review uninstall --dry-run and resume uninstall"
fi

if [ "${ACTION}" = rollback-library ]; then
    command -v fw_printenv >/dev/null 2>&1 || die "fw_printenv is required"
    lp_restore
    exit $?
fi

if [ "${ACTION}" = rollback ]; then
    runtime_files_check || die "Runtime rollback preflight failed"
    if [ "${RUNTIME_PRESENT}" = 1 ]; then
        worker_paths_check || die "Cannot safely stop the worker"
        lp_lock_worker || die "Worker is still finishing a vendor call; retry rollback later"
        runtime_files_remove || die "Runtime rollback incomplete; review and retry"
        lp_unlock_worker
    fi
    echo "Project runtime scripts removed. Setup records retained in ${D}/setup-state."
    echo "ONU identity, boot settings, MIB, saved TOML and libponnet are unchanged."
    echo "Binary restoration is a separate guarded rollback-library action. No reboot performed."
    exit 0
fi

case "${ACTION}" in
    check|install|patch-library)
        runtime_files_check || die "The runtime paths must be empty or contain this project's scripts"
        saved_paths_check || die "Unsafe configuration paths"
        worker_paths_check || die "Unsafe worker paths"
        ;;
    *) :;;
esac
for cmd in fw_printenv ip omci_pipe.sh pon tc; do
    command -v "${cmd}" >/dev/null 2>&1 || die "Required command missing: ${cmd}"
done
if [ "${CONFIG_SEEN}" = 0 ] && [ -n "${CONFIG_ENV_SET}" ] && [ -z "${CONFIG_INPUT}" ]; then
    die "Configuration path is explicitly empty; select a configuration path"
fi
CONFIG_INPUT=${CONFIG_INPUT:-"${D}/dei-pon.toml"}
[ -f "${CONFIG_INPUT}" ] && [ -r "${CONFIG_INPUT}" ] || die "Configuration must be a readable regular file: ${CONFIG_INPUT}; supply --config PATH"
CONFIG_SIZE=$(wc -c < "${CONFIG_INPUT}") || die "Cannot read configuration size"
[ "${CONFIG_SIZE}" -le 65536 ] || die "Configuration exceeds 64 KiB; no configuration changes"
cp "${CONFIG_INPUT}" "${S}/config.toml" || die "Cannot stage configuration"
chmod 600 "${S}/config.toml"
. "${S}/dei-config.sh"
dei_config_load "${S}/config.toml" || die "Invalid configuration; nothing changed"
echo "Configuration validated. Selected WAN VLAN: ${CFG_INTERNET_VLAN}"
echo "Encryption key ring: ${CFG_INTERNET_ENCRYPTION_KEY_RING}; library patch enabled: ${CFG_LIBRARY_PATCH_ENABLED}; policy: ${CFG_LIBRARY_PATCH_POLICY}"
case "${ACTION}" in install|patch-library) install_audit_start || die "Cannot create the private installation audit; no configuration changes";; *) :;; esac

lp_assess > "${S}/library-report.txt"
cat "${S}/library-report.txt"
install_audit_report library-assessment "${S}/library-report.txt" || die "Cannot record library assessment"
module_inventory > "${S}/inventory-report.txt"
cat "${S}/inventory-report.txt"
install_audit_report module-inventory "${S}/inventory-report.txt" || die "Cannot record module inventory"

echo "Live GEM discovery: selected VLAN -> current mapper -> current GEM members (no fixed range)."
if DEI_PON_CONFIG_PATH="${S}/config.toml" DEI_PON_CONF_DIR="${S}" sh "${S}/keyring.sh" --check 8>&- > "${S}/gem-report.txt" 2>&1; then
    LIVE_GEMS_READY=1
else
    LIVE_GEMS_READY=0
fi
cat "${S}/gem-report.txt"
install_audit_report live-gem-discovery "${S}/gem-report.txt" || die "Cannot record GEM discovery"
if [ "${LIVE_GEMS_READY}" != 1 ]; then
    echo "Live validation is pending; this does not authorize guessing a mapper or GEM."
fi

if [ "${ACTION}" = diagnose ]; then
    command -v sleep >/dev/null 2>&1 || die "sleep is required for repeated PON observations"
    module_pon_observe "${DIAG_SAMPLES}" "${DIAG_INTERVAL}"
    exit $?
fi

if [ "${ACTION}" = patch-library ]; then
    INSTALL_AUDIT_PHASE=binary-patch
    lp_patch
    PATCH_RESULT=$?
    install_audit_note "binary-operation applied=${LP_APPLIED:-0} exit=${PATCH_RESULT}" || die "Binary operation finished but its audit result could not be saved"
    exit "${PATCH_RESULT}"
fi

. "${S}/setup-engine.sh"
if setup_check > "${S}/setup-report.txt" 2>&1; then
    SETUP_PLAN_VALID=1
else
    SETUP_PLAN_VALID=0
fi
if [ "${SETUP_PLAN_VALID}" = 1 ]; then cat "${S}/setup-report.txt"; else cat "${S}/setup-report.txt" >&2; fi
install_audit_report setup-plan "${S}/setup-report.txt" || die "Cannot record setup plan"
[ "${SETUP_PLAN_VALID}" = 1 ] || die "Setup validation failed; no configuration changes"
if [ "${ACTION}" = install ]; then
    command -v fw_setenv >/dev/null 2>&1 || die "fw_setenv is required for setup installation"
    if [ "${LP_STATE}" != patched ] && { [ "${LP_RECOMMENDATION:-not-indicated}" = suggested ] || [ "${SETUP_PATCH_REQUIRED:-0}" = 1 ]; }; then
        if [ "${CFG_LIBRARY_PATCH_ENABLED}" != true ] || [ "${CFG_LIBRARY_PATCH_POLICY}" != auto ]; then
            INSTALL_AUDIT_PHASE=patch-opt-in-required
            echo 'Review the patch evidence, then explicitly set [library_patch] enabled = true and policy = "auto" in your TOML and rerun check/install.'
            install_audit_note 'blocked: library patch is suggested but explicit configuration opt-in is absent or forbidden' || die "Cannot record blocked patch decision"
            die "Library patch requires explicit opt-in; no configuration changes"
        fi
        if [ "${SETUP_PATCH_REQUIRED:-0}" != 1 ] && [ "${LP_LOG_EVIDENCE:-none}" = uni-mapping ]; then
            echo 'Log evidence is advisory: review the UNI/MIB profile if errors persist. The binary will remain unchanged unless the exact compatibility conditions apply.'
            install_audit_note 'log-only recommendation: configured UNI does not authorize the binary patch' || die "Cannot record patch recommendation"
        fi
    fi
fi

if [ "${ACTION}" = check ]; then
    setup_check_snapshot_reads "${S}/backup-read-error" || die "Cannot establish original firmware settings"
    echo "Check complete. No configuration, environment, MIB or binary changes were made."
    exit 0
fi

# Stop our old watcher cooperatively, using its original lock across upgrades.
INSTALL_AUDIT_PHASE=runtime-installation
lp_lock_worker || die "Worker still finishing a vendor call; no installation changes"
# Capture the firmware baseline before publishing the first runtime file.
setup_backup || die "Cannot verify original firmware settings; runtime files were not replaced"
runtime_files_check || die "Runtime files changed during installation"
saved_paths_check || die "Configuration paths changed during installation"
if [ -f "${D}/dei-pon.toml" ] && [ ! -e "${CONFIG_BEFORE}" ]; then
    atomic_copy "${D}/dei-pon.toml" "${CONFIG_BEFORE}" 600 || die "Cannot back up previous configuration"
fi
# Publish the hook last while the firmware and worker locks are held.
for name in ${FILES}; do
    runtime_file_check "${name}" || die "Runtime file changed during installation"
    if [ "${RUNTIME_FILE_STATE}" != current ]; then
        atomic_copy "${S}/${name}" "${D}/${name}" 755 || die "Cannot install ${name}"
    fi
done
atomic_copy "${S}/config.toml" "${D}/dei-pon.toml" 600 || die "Cannot save configuration"
lp_unlock_worker

INSTALL_AUDIT_PHASE=setup-application
setup_apply || die "Runtime files saved, but setup phase failed; inspect the reported error before rebooting"
if [ "${SETUP_CONFIG_READY:-0}" = 1 ]; then
    if [ "${SETUP_PATCH_REQUIRED:-0}" = 1 ]; then
        [ "${SETUP_MAINTENANCE_READY:-0}" = 1 ] || die "Binary patch requires the maintenance phase"
        [ "${CFG_LIBRARY_PATCH_ENABLED}" = true ] && [ "${CFG_LIBRARY_PATCH_POLICY}" = auto ] || die "Required binary patch has not been explicitly enabled"
        INSTALL_AUDIT_PHASE=binary-patch
        lp_patch || die "Conditional binary patch failed; maintenance state retained"
        [ "${LP_APPLIED:-0}" != 1 ] || SETUP_REBOOT_REQUIRED=1
    fi
    setup_finish || die "Could not complete the maintenance phase"
fi

if [ "${SETUP_REBOOT_REQUIRED:-0}" = 1 ]; then
    INSTALL_AUDIT_PHASE=reboot-required
    echo "A planned reboot is required for this setup phase. No reboot was performed."
    echo "After reboot, rerun: sh $0 install"
elif [ -e "${LP_ROOT}/ptconf/.failsafe" ]; then
    INSTALL_AUDIT_PHASE=maintenance-pending
    echo "Your firmware failsafe remains present; automatic runtime work is deferred until maintenance is complete."
elif [ "${CFG_WATCHER_ENABLED}" = true ] && [ "${DEI835_SKIP_START:-0}" != 1 ]; then
    INSTALL_AUDIT_PHASE=watcher-started
    DEI_PON_CONFIG_PATH="${D}/dei-pon.toml" DEI_PON_CONF_DIR="${D}" DEI_PON_RUN_DIR="${R}" sh "${D}/vlan_fixes_hook.sh" 8>&- || die "Installed, but watcher launch failed"
    echo "Watcher requested. Check: sh ${D}/status.sh"
elif [ "${CFG_WATCHER_ENABLED}" = false ] && [ "${DEI835_SKIP_START:-0}" != 1 ]; then
    INSTALL_AUDIT_PHASE=runtime-once
    DEI_PON_CONFIG_PATH="${D}/dei-pon.toml" DEI_PON_CONF_DIR="${D}" sh "${D}/keyring.sh" --once 8>&- || die "Setup saved; one-shot runtime application is pending. Retry after PON provisioning settles."
    echo "Runtime settings applied once; continuous watching is disabled."
else
    INSTALL_AUDIT_PHASE=configured
    echo "Configuration saved; watcher is disabled or startup was deferred."
fi
install_audit_note "setup reboot-required=${SETUP_REBOOT_REQUIRED:-0} patch-applied=${LP_APPLIED:-0}" || die "Setup finished but its audit result could not be saved"
echo "Saved configuration: ${D}/dei-pon.toml"
echo "Match the UDMP WAN VLAN to ${CFG_INTERNET_VLAN}. No automatic restart/reboot or hardware bridge-port writes were made."
""",
))
OUTPUT.mkdir(parents=True, exist_ok=True)
target = OUTPUT / "dei-pon-install.sh"
target.write_bytes("".join(parts).encode("utf-8"))
target.chmod(0o755)
config = OUTPUT / "dei-pon.toml"
config.write_bytes((ROOT / "config/dei-pon.example.toml").read_bytes())
config.chmod(0o644)
archive = OUTPUT / "dei-pon-setup.zip"
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
    for artifact, mode in ((target, 0o755), (config, 0o644)):
        info = zipfile.ZipInfo(artifact.name, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | mode) << 16
        bundle.writestr(info, artifact.read_bytes(), compresslevel=9)
checksums = "".join(
    f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n" for artifact in (target, config, archive)
)
(OUTPUT / "SHA256SUMS").write_bytes(checksums.encode("ascii"))
print(f"Built {target.relative_to(ROOT)} and {archive.relative_to(ROOT)}")
