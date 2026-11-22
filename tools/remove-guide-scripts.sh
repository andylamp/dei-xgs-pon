#!/bin/sh
# One-time cleanup for the three guide scripts identified on a WAS-110.
# This file runs independently from the installer and uses only BusyBox tools.
set -u
umask 077

usage() { echo "Usage: sh remove-guide-scripts.sh [--dry-run|--apply]"; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}
case "$#:${1:-}" in
    0: | 1:--dry-run) apply=0 ;;
    1:--apply) apply=1 ;;
    1:--help | 1:-h)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

root=${DEI835_MODULE_ROOT:-}
config_dir="${root}/ptconf/8311"
lock="${root}/tmp/8311-fix-vlans.lock"
files='vlan_fixes_hook.sh keyring.sh status.sh'

exists() { [ -e "$1" ] || [ -L "$1" ]; }

verify_file() {
    path="${config_dir}/$1"
    [ -f "${path}" ] && [ ! -L "${path}" ] || die "Expected a regular file: ${path}"
    case "$1" in
        vlan_fixes_hook.sh) expected=a0a56570e02db55880888fa0aac85bf6a26aa11060a840cd2ea212f69a02366b ;;
        keyring.sh) expected=af46d3f383b7262e9f7fe4e6718973c2a036fa49e0ea9eb2fb0bff7615eebba7 ;;
        status.sh) expected=1f362bcedcacf0ed44a7e5318b2c530c521d18be64dea99c589d94f07a92dca8 ;;
        *) die 'Unexpected cleanup target' ;;
    esac
    record=$(sha256sum "${path}") || die "Cannot verify ${path}"
    [ "${record%% *}" = "${expected}" ] || die "Unrecognized or edited ${path}; review it manually"
}

preflight() {
    count=0
    [ -d "${config_dir}" ] && [ ! -L "${config_dir}" ] || die "Invalid configuration directory: ${config_dir}"
    if exists "${config_dir}/lctfix.sh"; then
        die "Optional lctfix.sh has no verified checksum; review it manually before cleanup"
    fi
    for name in ${files}; do
        exists "${config_dir}/${name}" || continue
        verify_file "${name}"
        count=$((count + 1))
    done
    if [ "${count}" -gt 0 ]; then
        for name in dei-835-lib.sh dei-config.sh; do
            if exists "${config_dir}/${name}"; then die "Mixed installation (${name} present); review before cleanup"; fi
        done
    fi
}

check_helpers() {
    [ -d "${root}/proc" ] || die 'Cannot inspect running helpers'
    if ! command -v tr >/dev/null || ! command -v awk >/dev/null; then die 'Cannot inspect helper arguments'; fi
    # Helpers may outlive the hook that started them. Match complete process
    # arguments without displaying command lines, signalling, or stopping them.
    for cmdline in "${root}"/proc/[0-9]*/cmdline; do
        [ -r "${cmdline}" ] || continue
        if tr '\000' '\n' <"${cmdline}" 2>/dev/null | awk '
            /^(\/ptconf\/8311\/|\.\/)?(keyring|lctfix|vlan_fixes_hook)\.sh$/ {active=1}
            END {exit !active}'; then
            die 'Guide helper or hook is active; wait for it to finish, then retry'
        fi
    done
}

[ -d "${root}/ptconf" ] && [ ! -L "${root}/ptconf" ] || die 'Expected the module ptconf directory'
if ! exists "${config_dir}"; then
    echo 'No active guide scripts to remove.'
    exit 0
fi
preflight
if [ "${count}" = 0 ]; then
    echo 'No active guide scripts to remove.'
    exit 0
fi
if [ "${apply}" = 0 ]; then
    check_helpers
    for name in ${files}; do
        if exists "${config_dir}/${name}"; then printf 'Would remove %s/%s\n' "${config_dir}" "${name}"; fi
    done
    echo 'Preview complete. Use --apply to remove these verified files.'
    exit 0
fi

# Use the same lock as 8311-vlansd while verifying and unlinking its hook.
if exists "${lock}" && { [ -L "${lock}" ] || [ ! -f "${lock}" ]; }; then die 'Invalid firmware lock path'; fi
exec 8>>"${lock}"
flock -n 8 || die 'Firmware hook is busy; retry after it finishes'
preflight
check_helpers
for name in ${files}; do
    exists "${config_dir}/${name}" || continue
    verify_file "${name}"
    rm "${config_dir}/${name}" || die "Could not remove ${name}; review and retry"
    printf 'Removed %s/%s\n' "${config_dir}" "${name}"
done
