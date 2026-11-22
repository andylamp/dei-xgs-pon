#!/bin/sh
# Reverse verified installer writes. Loaded from the checked installer payload.

uninstall_env_error() {
    printf 'ERROR: %s\n' "$1" >&2
    return 1
}

uninstall_env_file() { [ -f "$1" ] && [ ! -L "$1" ]; }
uninstall_env_exists() { [ -e "$1" ] || [ -L "$1" ]; }

# Compare the observed state to the exact saved bytes, including present-empty.
# The journal stores one terminating newline; the before-image stores none.
uninstall_env_key() {
    un_key=$1
    un_original_prefix="${SU_BACKUP:?setup paths required}/${un_key}"
    un_receipt="${SU_BACKUP}/applied-v1/${un_key}"
    UN_RESTORE=0
    # Verify the before-image before interpreting any recorded value.
    uninstall_env_file "${un_original_prefix}.state" || return 1
    if printf 'absent\n' | cmp -s - "${un_original_prefix}.state"; then
        ! uninstall_env_exists "${un_original_prefix}.value" || return 1
        un_original_state=absent
        un_original_value=
    elif printf 'present\n' | cmp -s - "${un_original_prefix}.state"; then
        uninstall_env_file "${un_original_prefix}.value" || return 1
        un_original_state=present
        un_original_value=$(cat "${un_original_prefix}.value") &&
            printf '%s' "${un_original_value}" | cmp -s - "${un_original_prefix}.value" || return 1
    else
        return 1
    fi
    if ! uninstall_env_exists "${un_receipt}" && ! uninstall_env_exists "${un_receipt}.pending"; then return 0; fi

    case "${un_key}" in
        bootcmd | 8311_persist_root)
            uninstall_env_error 'Root-overlay boot settings were managed; review persistence recovery first'
            return 1
            ;;
        mib_file | 8311_mib_file)
            # Other MIBs may need a different library; the original file is not
            # identified by a firmware-variable snapshot of its pathname.
            case "${un_original_value}" in
                '' | /etc/mibs/prx300_1U.ini) ;;
                prx300_1U.ini) [ "${un_key}" = 8311_mib_file ] || return 1 ;;
                *)
                    uninstall_env_error 'Original MIB selection requires manual compatibility review'
                    return 1
                    ;;
            esac
            for un_pointer in "${un_receipt}" "${un_receipt}.pending"; do
                uninstall_env_exists "${un_pointer}" || continue
                printf '%s\n' /etc/mibs/prx300_1U.ini | cmp -s - "${un_pointer}" || {
                    uninstall_env_error 'A non-native MIB was selected; review MIB recovery first'
                    return 1
                }
            done
            [ ! -L "${SU_LIB:?}" ] && setup_md5_matches "${SU_LIB}" "${SU_STOCK_HASH:?}" &&
                lp_resolve_module_path /etc/mibs/prx300_1U.ini && [ -f "${LP_RESOLVED_PATH:?}" ] &&
                un_native_uni=$(setup_uni "${LP_RESOLVED_PATH}") && [ "${un_native_uni}" = 101 ] || {
                uninstall_env_error 'Restoring MIB selectors requires the supported stock library and sole native UNI'
                return 1
            }
            ;;
        *) ;;
    esac
    setup_read_env_exact "${un_key}" "${S:?private staging directory required}/uninstall-read-error" || return 1
    if [ "${SE_ENV_STATE:?}" = "${un_original_state}" ] && [ "${SE_ENV_VALUE:-}" = "${un_original_value}" ]; then
        return 0 # Already original, including after a partially completed uninstall.
    fi
    if [ "${SE_ENV_STATE}" = present ]; then
        for un_expected in "${un_receipt}" "${un_receipt}.pending"; do
            uninstall_env_file "${un_expected}" || continue
            if printf '%s\n' "${SE_ENV_VALUE}" | cmp -s - "${un_expected}"; then
                UN_RESTORE=1
                return 0
            fi
        done
    fi
    uninstall_env_error "Firmware setting ${un_key} differs from its original and installer-written values; review manually"
}

uninstall_env_plan() {
    setup_paths
    UN_ENV_KEYS=
    for un_marker in "${SU_PENDING:?}" "${SU_PERSIST_PENDING:?}" "${SU_FS_OWNER:?}" "${LP_MARKER:?}" "${LP_BACKUP:?}" "${SU_STATE:?}"/mib.*.original; do
        if uninstall_env_exists "${un_marker}"; then
            uninstall_env_error 'MIB, binary or maintenance recovery is pending; preserve the recovery state for review'
            return 1
        fi
    done
    if ! uninstall_env_exists "${SU_STATE}"; then
        echo 'No setup-state backup is present; no firmware settings to restore from installer records.'
        return 0
    fi
    un_journal="${SU_BACKUP:?}/applied-v1"
    if [ -L "${SU_STATE}" ] || [ ! -d "${SU_STATE}" ] || [ -L "${SU_BACKUP}" ] || [ ! -d "${SU_BACKUP}" ] ||
        ! uninstall_env_file "${SU_BACKUP}/owner" || ! printf 'dei-setup-v2\n' | cmp -s - "${SU_BACKUP}/owner" ||
        [ ! -d "${un_journal}" ] || [ -L "${un_journal}" ]; then
        uninstall_env_error 'Setup backup has no verified installer write journal; manual firmware recovery is required'
        return 1
    fi
    un_has_writes=0
    for un_entry in "${un_journal}"/* "${un_journal}"/.[!.]* "${un_journal}"/..?*; do
        uninstall_env_exists "${un_entry}" || continue
        un_name=${un_entry##*/}
        un_name=${un_name%.pending}
        case "${un_name}" in '' | *[!a-zA-Z0-9_]*)
            uninstall_env_error 'Invalid environment write record name'
            return 1
            ;;
        *) ;; esac
        case " ${SU_VARS:?setup paths required} " in
            *" ${un_name} "*) uninstall_env_file "${un_entry}" || {
                uninstall_env_error 'Invalid environment write record'
                return 1
            } ;;
            *)
                uninstall_env_error 'Unexpected environment write record'
                return 1
                ;;
        esac
        un_has_writes=1
    done
    # An interrupted installation can leave a before-image before saving TOML
    # or writing any variables. All other installs need an identifiable profile.
    if uninstall_env_exists "${D:?}/dei-pon.toml" || [ "${un_has_writes}" = 1 ]; then
        if ! uninstall_env_file "${D}/dei-pon.toml" || ! dei_config_load "${D}/dei-pon.toml"; then
            uninstall_env_error 'Cannot verify the installed MIB profile from the saved configuration'
            return 1
        fi
        if [ "${CFG_ONU_MIB_PROFILE:?}" = dei_0205 ]; then
            uninstall_env_error 'The generated UNI-0x0205 MIB requires separate MIB/library recovery'
            return 1
        fi
    fi
    for un_name in ${SU_VARS}; do
        uninstall_env_key "${un_name}" || {
            uninstall_env_error "Cannot verify original or current state for ${un_name}"
            return 1
        }
        if [ "${UN_RESTORE}" = 1 ]; then
            UN_ENV_KEYS="${UN_ENV_KEYS} ${un_name}"
            printf 'Firmware setting to restore: %s (%s).\n' "${un_name}" "${un_original_state}"
        fi
    done
}

uninstall_env_apply() {
    # Revalidate all keys under both locks before making the first write.
    uninstall_env_plan >/dev/null || return 1
    for un_name in ${UN_ENV_KEYS}; do
        uninstall_env_key "${un_name}" || return 1
        [ "${UN_RESTORE}" = 1 ] || continue
        if [ "${un_original_state}" = absent ]; then
            fw_setenv "${un_name}" >/dev/null 2>&1 || {
                uninstall_env_error "Cannot unset ${un_name}"
                return 1
            }
        else
            fw_setenv "${un_name}" "${un_original_value}" >/dev/null 2>&1 || {
                uninstall_env_error "Cannot restore ${un_name}"
                return 1
            }
        fi
        if ! setup_read_env_exact "${un_name}" "${S}/uninstall-read-error" ||
            [ "${SE_ENV_STATE}" != "${un_original_state}" ] || [ "${SE_ENV_VALUE}" != "${un_original_value}" ]; then
            uninstall_env_error "Original firmware setting ${un_name} could not be verified after restoration"
            return 1
        fi
        printf 'Restored firmware setting: %s.\n' "${un_name}"
    done
}
