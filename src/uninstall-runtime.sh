#!/bin/sh
# Installer-only removal of active runtime files. The caller holds both locks
# during removal and restores any eligible boot settings before retiring state.

uninstall_runtime_error() {
    printf 'ERROR: %s\n' "$*" >&2
    return 1
}

uninstall_recovery_check() {
    UNINSTALL_ARCHIVE=
    ur_marker="${D:?}/.dei-uninstall-archive"
    regular_or_absent "${ur_marker}" || return 1
    [ -f "${ur_marker}" ] || return 0
    # A saved basename can only identify a private sibling created by mktemp.
    ur_basename=$(cat "${ur_marker}") || return 1
    ur_suffix=${ur_basename#dei-uninstalled.}
    case "${ur_basename}" in dei-uninstalled.??????) ;; *) return 1 ;; esac
    case "${ur_suffix}" in *[!A-Za-z0-9]*) return 1 ;; *) ;; esac
    UNINSTALL_ARCHIVE="${D}/${ur_basename}"
    [ -d "${UNINSTALL_ARCHIVE}" ] && [ ! -L "${UNINSTALL_ARCHIVE}" ] || return 1
    ur_mode=$(stat -c %a "${UNINSTALL_ARCHIVE}") && [ "${ur_mode}" = 700 ] || return 1
    regular_or_absent "${UNINSTALL_ARCHIVE}/owner" && [ -f "${UNINSTALL_ARCHIVE}/owner" ] || return 1
    ur_owner=$(cat "${UNINSTALL_ARCHIVE}/owner") && [ "${ur_owner}" = "dei-uninstall-v1 ${ur_basename}" ] || return 1
    for ur_item in setup-state dei-pon.toml; do
        # A retry may find an already moved item, never replace it.
        [ ! -L "${UNINSTALL_ARCHIVE}/${ur_item}" ] || return 1
        if [ -e "${UNINSTALL_ARCHIVE}/${ur_item}" ] && [ -e "${D}/${ur_item}" ]; then return 1; fi
    done
}

uninstall_runtime_check() {
    UNINSTALL_RUNTIME_WORK=0
    uninstall_recovery_check || {
        uninstall_runtime_error 'Invalid or conflicting pending uninstall archive'
        return 1
    }
    if [ -n "${UNINSTALL_ARCHIVE}" ]; then UNINSTALL_RUNTIME_WORK=1; fi
    runtime_files_check || return 1
    saved_paths_check || return 1
    if [ "${RUNTIME_PRESENT:?}" = 1 ]; then UNINSTALL_RUNTIME_WORK=1; fi
    if [ -e "${D}/setup-state" ] || [ -L "${D}/setup-state" ]; then
        # uninstall_env_plan validates ownership and the stored firmware records.
        UNINSTALL_RUNTIME_WORK=1
    fi
    if [ "${UNINSTALL_RUNTIME_WORK}" = 1 ]; then worker_paths_check || return 1; fi
}

# Publish a private recovery path before moving the first item. If an individual
# rename fails, a retry can finish moving the original state into the same archive.
uninstall_archive_state() {
    if [ -z "${UNINSTALL_ARCHIVE:-}" ]; then
        if [ ! -d "${D:?}/setup-state" ]; then
            if [ -f "${D}/dei-pon.toml" ]; then
                echo 'Preserved dei-pon.toml: no installer backup establishes ownership.'
            fi
            return 0
        fi
        UNINSTALL_ARCHIVE=$(mktemp -d "${D}/dei-uninstalled.XXXXXX") || return 1
        printf 'dei-uninstall-v1 %s\n' "${UNINSTALL_ARCHIVE##*/}" >"${UNINSTALL_ARCHIVE}/owner" || return 1
        ur_marker_stage=$(mktemp "${D}/.dei-uninstall-marker.XXXXXX") || return 1
        if ! printf '%s\n' "${UNINSTALL_ARCHIVE##*/}" >"${ur_marker_stage}" ||
            ! mv "${ur_marker_stage}" "${D}/.dei-uninstall-archive"; then
            rm -f "${ur_marker_stage}"
            return 1
        fi
    fi
    for ur_item in setup-state dei-pon.toml; do
        [ -e "${D}/${ur_item}" ] || continue
        if [ -e "${UNINSTALL_ARCHIVE}/${ur_item}" ] || [ -L "${UNINSTALL_ARCHIVE}/${ur_item}" ]; then
            uninstall_runtime_error 'Conflicting uninstall archive entry; recovery data preserved'
            return 1
        fi
        mv "${D}/${ur_item}" "${UNINSTALL_ARCHIVE}/${ur_item}" || return 1
    done
    rm "${D}/.dei-uninstall-archive" || return 1
    echo "Inactive recovery files retained in ${UNINSTALL_ARCHIVE}"
}

uninstall_runtime_remove() {
    uninstall_runtime_check || return 1
    runtime_files_remove || return 1
    uninstall_archive_state
}
