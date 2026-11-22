#!/bin/sh
# Phased, configuration-driven setup. Source after dei_config_load.
# Public: setup_check, setup_apply, setup_finish; SETUP_* result flags.
# No live device-protocol changes, process restarts, reboots or binary writes.

setup_error() {
    printf 'ERROR: %s\n' "$*" >&2
    return 1
}
setup_env() {
    # Optional settings use an empty value when the firmware read fails.
    su_env_read=$(fw_printenv -n "$1" 2>/dev/null) || return 0
    printf '%s\n' "${su_env_read}"
}
setup_env_matches() {
    su_env_value=$(setup_env "$1")
    [ "${su_env_value}" = "$2" ]
}
# Read a single U-Boot variable for reversible writes. Only the firmware's
# explicit undefined-variable error establishes absence; I/O failures abort.
setup_read_env_exact() {
    if SE_ENV_VALUE=$(LC_ALL=C fw_printenv -n "$1" 2>"$2"); then
        [ ! -s "$2" ] || return 1
        SE_ENV_STATE=present
    else
        [ -z "${SE_ENV_VALUE}" ] && printf '## Error: "%s" not defined\n' "$1" | cmp -s - "$2" || return 1
        SE_ENV_STATE=absent
    fi
}
setup_md5_matches() {
    su_actual_md5=$(md5 "$1") && [ "${su_actual_md5}" = "$2" ]
}
setup_paths() {
    SU_ROOT=${LP_ROOT:-${DEI835_MODULE_ROOT:-}}
    SU_STATE="${D:?installer configuration directory is required}/setup-state"
    SU_BACKUP="${SU_STATE}/fwenv.originals"
    SU_FS="${SU_ROOT}/ptconf/.failsafe"
    SU_FS_OWNER="${SU_STATE}/failsafe.owner"
    SU_PENDING="${SU_STATE}/maintenance.pending"
    SU_PERSIST="${SU_ROOT}/etc/dei-setup-persist"
    SU_PERSIST_PENDING="${SU_STATE}/persistence.pending"
    SU_BOOT=$(cat "${SU_ROOT}/proc/sys/kernel/random/boot_id" 2>/dev/null || true)
    SU_LIB="${SU_ROOT}/usr/lib/libponnet.so.0.0.0"
    SU_STOCK_HASH=5326f83e22a18929b830adfb82cfc483
    SU_PATCH_HASH=5ddb892fbf0b648479f605d9fddda50c
    SU_ROM_HASH=924ac47bce92553361928dd3fa61bec7
    SU_DEI_HASH=1573d7b59c093751df3531e8b603d3b5
    SU_BOOT_PERSIST='run ubi_init; run flash_flash'
    SU_VARS='8311_gpon_sn 8311_omcc_version 8311_vendor_id 8311_equipment_id 8311_hw_ver mib_file 8311_mib_file 8311_fix_vlans 8311_failsafe_delay 8311_persist_root bootcmd 8311_ipaddr 8311_netmask 8311_gateway 8311_ping_ip'
}
setup_running() {
    for su_comm in "${SU_ROOT}"/proc/[0-9]*/comm; do
        [ -f "${su_comm}" ] || continue
        case "$(cat "${su_comm}" 2>/dev/null)" in omcid | omcid.sh) return 0 ;; *) ;; esac
        if [ -r "${su_comm%/comm}/cmdline" ]; then
            tr '\000' ' ' <"${su_comm%/comm}/cmdline" | grep -Eq '(^|[ /])omcid\.sh([[:space:]]|$)' && return 0
        fi
    done
    return 1
}
setup_storage() {
    setup_env_matches 8311_persist_root 1 &&
        setup_env_matches bootcmd "${SU_BOOT_PERSIST}" || return 1
    # The library patcher performs the stricter UBIFS/upperdir safety audit.
    if command -v lp_storage >/dev/null 2>&1; then lp_storage; else
        awk '$2=="/" && ($3=="overlay" || $3=="overlayfs") && $4 ~ /(^|,)rw(,|$)/ {ok=1} END {exit !ok}' "${SU_ROOT}/proc/mounts" 2>/dev/null
    fi
}
setup_read_persistence_markers() {
    [ -f "${SU_PERSIST_PENDING}" ] && [ ! -L "${SU_PERSIST_PENDING}" ] &&
        [ -f "${SU_PERSIST}" ] && [ ! -L "${SU_PERSIST}" ] || return 1
    su_pending_mark=$(cat "${SU_PERSIST_PENDING}") && su_mark=$(cat "${SU_PERSIST}") &&
        [ -n "${su_mark}" ] && [ "${su_mark}" = "${su_pending_mark}" ]
}
setup_persistence_proven() {
    [ -n "${SU_BOOT}" ] || return 1
    if [ -f "${SU_PERSIST_PENDING}" ]; then
        setup_read_persistence_markers &&
            [ "${su_mark}" != "${SU_BOOT}" ] && setup_storage
    else
        # An already patched library still requires verified persistent backing.
        [ "${SU_LIBRARY_HASH}" = "${SU_PATCH_HASH}" ] && setup_storage
    fi
}
setup_uni() {
    awk '/^[[:space:]]*#/ {next}
        {n=1; if($1=="!" || $1=="?") n=2
         if($n=="11") {count++; if(n==2) conditional++; id=tolower($(n+1)); if(id=="0x0205" || id=="0x205" || id=="517") target++; if(id=="0x0101" || id=="0x101" || id=="257") native++}}
        END {if(count==1 && target==1 && !conditional) print "205"; else if(count==1 && native==1 && !conditional) print "101"; else if(count==1 && !conditional) print "other"; else print "ambiguous"}' "$1"
}
setup_value() { if [ -n "$2" ]; then printf '%s' "$2"; else setup_env "$1"; fi; }
setup_identity_value() {
    SU_IDENTITY_VALUE=$(setup_value "$1" "$2")
    su_identity_bytes=$(printf '%s' "${SU_IDENTITY_VALUE}" | wc -c)
    if [ "${su_identity_bytes}" -gt "$3" ]; then
        if [ -n "$2" ]; then
            setup_error "The explicit $1 identity exceeds the firmware byte limit"
            return 1
        fi
        # Match the tagged firmware getters without rewriting inherited fwenv.
        SU_IDENTITY_VALUE=$(printf '%s' "${SU_IDENTITY_VALUE}" | head -c "$3")
        printf 'Setup: inherited %s is truncated to %s bytes in the generated MIB, matching firmware behavior (value hidden).\n' "$1" "$3"
    fi
    case "${SU_IDENTITY_VALUE}" in *\"* | *\\* | *'
'*)
        setup_error "The effective $1 identity cannot contain quotes, backslashes or control bytes"
        return 1
        ;;
    *) ;;
    esac
    if printf '%s' "${SU_IDENTITY_VALUE}" | LC_ALL=C grep -q '[[:cntrl:]]'; then
        setup_error "The effective $1 identity cannot contain quotes, backslashes or control bytes"
        return 1
    fi
}
setup_identity_field() {
    # Escape replacement syntax; identity values are quoted MIB fields.
    su_replacement=$(printf '"%s"' "$4" | sed 's/[\\&#]/\\&/g')
    sed -r "s#^(([!?][[:space:]]+)?$2([[:space:]]+(\"[^\"]*\"|[^\"[:space:]]+)){$3}[[:space:]]+)(\"[^\"]*\"|[^\"[:space:]]+)#\\1${su_replacement}#g" "$1" >"$1.edit" || return 1
    mv -f "$1.edit" "$1"
}
setup_build_mib() {
    # Only the exact read-only image template is supported; never mutate it.
    su_output=$1
    su_source="${SU_ROOT}/rom/etc/mibs/prx300_1U.ini"
    setup_md5_matches "${su_source}" "${SU_ROM_HASH}" || {
        setup_error 'The DEI MIB profile requires the known v2.8.3 ROM template'
        return 1
    }
    cp "${su_source}" "${su_output}" || return 1
    sed -i -e 's/ 0x0101000\([0-7]\) / 0x0205000\1 /' \
        -e 's/^11 0x0101 /11 0x0205 /' -e 's/^5 0x0101 /5 0x0201 /' \
        -e 's/^6 0x0101 /6 0x0201 /' -e 's/^264 0x0101 /264 0x0205 /' "${su_output}" || return 1
    setup_md5_matches "${su_output}" "${SU_DEI_HASH}" || {
        setup_error 'DEI UNI transformation did not produce the known template'
        return 1
    }
    su_vendor=$(setup_value 8311_vendor_id "${CFG_ONU_VENDOR_ID:-}")
    [ -n "${su_vendor}" ] || su_vendor=$(printf '%s' "${SU_SERIAL}" | cut -c1-4)
    if [ "${#su_vendor}" -ne 4 ] || ! printf '%s\n' "${su_vendor}" | LC_ALL=C grep -Eq '^[A-Za-z0-9]{4}$'; then
        setup_error 'The effective 8311_vendor_id identity must contain four alphanumeric characters'
        return 1
    fi
    setup_identity_value 8311_hw_ver "${CFG_ONU_HARDWARE_VERSION:-}" 14 || return 1
    su_hardware=${SU_IDENTITY_VALUE}
    setup_identity_value 8311_equipment_id "${CFG_ONU_EQUIPMENT_ID:-}" 20 || return 1
    su_equipment=${SU_IDENTITY_VALUE}
    setup_identity_field "${su_output}" 6 5 "${su_vendor}" && setup_identity_field "${su_output}" 256 1 "${su_vendor}" || return 1
    if [ -n "${su_hardware}" ]; then
        setup_identity_field "${su_output}" 256 2 "${su_hardware}" || return 1
        if setup_env_matches 8311_cp_hw_ver_sync 1; then setup_identity_field "${su_output}" 6 4 "${su_hardware}" || return 1; fi
    fi
    if [ -n "${su_equipment}" ]; then setup_identity_field "${su_output}" 257 1 "${su_equipment}" || return 1; fi
    su_generated_uni=$(setup_uni "${su_output}") && [ "${su_generated_uni}" = 205 ]
}
setup_plan() {
    setup_paths
    SETUP_REBOOT_REQUIRED=0
    SETUP_MAINTENANCE_READY=0
    SETUP_CONFIG_READY=0
    SETUP_PATCH_REQUIRED=0
    SU_MIB_CHANGE=0
    SU_MAINTENANCE=0
    SU_NEED_PERSIST=0
    SU_PERSIST_CHANGE=0
    [ -d "${SU_ROOT}/ptconf" ] || {
        setup_error '/ptconf is unavailable'
        return 1
    }
    SU_SERIAL=$(setup_value 8311_gpon_sn "${CFG_ONU_SERIAL:-}")
    printf '%s\n' "${SU_SERIAL}" | grep -Eq '^[A-Za-z0-9]{4}[0-9A-Fa-f]{8}$' || {
        setup_error 'Provide the provisioned ONU serial in TOML; no valid existing serial is available'
        return 1
    }
    SU_PROFILE=${CFG_ONU_MIB_PROFILE:-preserve}
    SU_LIBRARY_HASH=$(md5 "${SU_LIB}") || SU_LIBRARY_HASH=unknown
    SU_IDENTITY_FIELDS_CHANGE=0
    SU_DERIVED_VENDOR_CHANGE=0
    if { [ -n "${CFG_ONU_VENDOR_ID:-}" ] && ! setup_env_matches 8311_vendor_id "${CFG_ONU_VENDOR_ID}"; } ||
        { [ -n "${CFG_ONU_EQUIPMENT_ID:-}" ] && ! setup_env_matches 8311_equipment_id "${CFG_ONU_EQUIPMENT_ID}"; } ||
        { [ -n "${CFG_ONU_HARDWARE_VERSION:-}" ] && ! setup_env_matches 8311_hw_ver "${CFG_ONU_HARDWARE_VERSION}"; }; then SU_IDENTITY_FIELDS_CHANGE=1; fi
    if [ -n "${CFG_ONU_SERIAL:-}" ] && setup_env_matches 8311_vendor_id ''; then
        su_old_vendor=$(setup_env 8311_gpon_sn | cut -c1-4)
        su_new_vendor=$(printf '%s' "${CFG_ONU_SERIAL}" | cut -c1-4)
        [ "${su_old_vendor}" = "${su_new_vendor}" ] || SU_DERIVED_VENDOR_CHANGE=1
    fi
    case "${SU_PROFILE}" in
        preserve)
            SU_MIB=$(setup_env mib_file)
            if [ -z "${SU_MIB}" ]; then
                SU_MIB=$(setup_env 8311_mib_file)
                case "${SU_MIB}" in /etc/mibs/*) ;; *) SU_MIB= ;; esac
            fi
            [ -n "${SU_MIB}" ] || {
                setup_error 'No explicit MIB is selected; choose native_1u or dei_0205 in TOML for a fresh installation'
                return 1
            }
            case "${SU_MIB}" in /*) ;; *)
                setup_error 'The selected MIB must be an absolute module path'
                return 1
                ;;
            esac
            [ -r "${SU_ROOT}${SU_MIB}" ] || {
                setup_error 'The selected MIB is missing or unreadable'
                return 1
            }
            SU_UNI=$(setup_uni "${SU_ROOT}${SU_MIB}")
            case "${SU_MIB}" in /etc/mibs/*)
                su_identity_mib=$(setup_env 8311_mib_file)
                su_identity_mib=${su_identity_mib:-/etc/mibs/prx300_1U.ini}
                if [ "${su_identity_mib}" != "${SU_MIB}" ] && { [ "${SU_IDENTITY_FIELDS_CHANGE}" = 1 ] || [ "${SU_DERIVED_VENDOR_CHANGE}" = 1 ]; }; then
                    setup_error 'The preserved MIB differs from the firmware identity-update target; select an explicit MIB profile before changing identity'
                    return 1
                fi
                ;;
            *)
                if [ "${SU_IDENTITY_FIELDS_CHANGE}" = 1 ]; then
                    setup_error 'Changing vendor, equipment or hardware identity with a preserved custom MIB would leave stale fields; select an explicit MIB profile'
                    return 1
                fi
                if [ "${SU_DERIVED_VENDOR_CHANGE}" = 1 ]; then
                    setup_error 'Changing the serial vendor prefix with a preserved custom MIB would leave stale vendor fields; select an explicit MIB profile'
                    return 1
                fi
                ;;
            esac
            ;;
        native_1u)
            SU_MIB=/etc/mibs/prx300_1U.ini
            [ -r "${SU_ROOT}${SU_MIB}" ] || {
                setup_error 'The native 1U MIB is unavailable'
                return 1
            }
            SU_UNI=$(setup_uni "${SU_ROOT}${SU_MIB}")
            [ "${SU_UNI}" = 101 ] || {
                setup_error 'The native 1U MIB does not have a supported single native UNI'
                return 1
            }
            setup_env_matches mib_file "${SU_MIB}" || SU_MIB_CHANGE=1
            setup_env_matches 8311_mib_file "${SU_MIB}" || SU_MIB_CHANGE=1
            ;;
        dei_0205)
            SU_MIB="${D#"${SU_ROOT}"}/dei.ini"
            [ ! -L "${SU_ROOT}${SU_MIB}" ] || {
                setup_error 'Refusing a symlinked generated MIB'
                return 1
            }
            su_temp=$(mktemp -d /tmp/dei-mib-check.XXXXXX) || return 1
            if ! setup_build_mib "${su_temp}/dei.ini"; then
                rm -rf "${su_temp}"
                return 1
            fi
            cmp -s "${su_temp}/dei.ini" "${SU_ROOT}${SU_MIB}" || SU_MIB_CHANGE=1
            rm -rf "${su_temp}"
            setup_env_matches mib_file "${SU_MIB}" || SU_MIB_CHANGE=1
            SU_UNI=205
            [ "${SU_MIB_CHANGE}" = 0 ] || SU_MAINTENANCE=1
            ;;
        *)
            setup_error 'Unsupported MIB profile'
            return 1
            ;;
    esac
    if [ "${SU_LIBRARY_HASH}" = "${SU_PATCH_HASH}" ] && [ "${SU_UNI}" != 205 ]; then
        if [ "${SU_UNI}" = 101 ]; then
            setup_error 'Native UNI 0x0101 is incompatible with the existing UNI-0x0205 binary patch; use a separately guarded library restoration first'
        else
            setup_error 'The existing patched library is supported only with a verified sole UNI 0x0205; the selected MIB layout is unverified, and no automatic library restoration is permitted'
        fi
        return 1
    fi
    if [ "${SU_UNI}" = 205 ]; then
        case "${SU_LIBRARY_HASH}" in
            "${SU_PATCH_HASH}") ;;
            "${SU_STOCK_HASH}")
                SETUP_PATCH_REQUIRED=1
                SU_MAINTENANCE=1
                ;;
            *)
                setup_error 'UNI 0x0205 requires a known stock or patched library; the current library is unsupported'
                return 1
                ;;
        esac
    fi
    case "${CFG_STORAGE_PERSISTENT_ROOT:-auto}" in
        enabled) SU_NEED_PERSIST=1 ;;
        auto) if [ "${SETUP_PATCH_REQUIRED}" = 1 ] || [ "${SU_LIBRARY_HASH}" = "${SU_PATCH_HASH}" ]; then SU_NEED_PERSIST=1; fi ;;
        preserve)
            if [ "${SETUP_PATCH_REQUIRED}" = 1 ] && ! setup_storage; then
                setup_error 'Binary patching needs persistent storage; select auto or enabled in TOML'
                return 1
            fi
            ;;
        *)
            setup_error 'Unsupported persistent-root policy'
            return 1
            ;;
    esac
    if [ "${SU_NEED_PERSIST}" = 1 ]; then
        if ! setup_env_matches 8311_persist_root 1 || ! setup_env_matches bootcmd "${SU_BOOT_PERSIST}"; then
            su_boot_command=$(setup_env bootcmd | sed -r 's/;[[:space:]]*/; /g; s/[[:space:]]+$//')
            case "${su_boot_command}" in '' | 'run ubi_init; ubi remove rootfs_data; run flash_flash' | 'run ubi_init; run flash_flash') ;; *)
                setup_error 'Custom bootcmd is not a recognized guide command; refusing to replace it'
                return 1
                ;;
            esac
            SU_PERSIST_CHANGE=1
        elif ! setup_storage; then
            setup_error 'Persistent-root boot settings are set, but a persistent writable root backing is not verified; inspect the mount layout before installing'
            return 1
        fi
    fi
    if [ -f "${SU_PERSIST_PENDING}" ]; then
        if [ ! -f "${SU_PERSIST}" ]; then
            setup_error 'The overlay marker did not survive the reboot or was removed; persistence is not proven'
            return 1
        fi
        if ! setup_read_persistence_markers; then
            setup_error 'Persistence markers disagree or are symlinked; preserve them for inspection'
            return 1
        fi
    fi
    if [ "${SU_MAINTENANCE}" = 1 ] || [ "${SU_PERSIST_CHANGE}" = 1 ]; then
        [ -n "${SU_BOOT}" ] || {
            setup_error 'A boot ID is required to verify the maintenance phases'
            return 1
        }
    fi
    return 0
}
setup_check() {
    setup_plan || return 1
    printf 'Setup: ONU serial available (value hidden); MIB profile=%s; UNI=%s\n' "${SU_PROFILE}" "${SU_UNI}"
    printf 'Setup: binary patch needed=%s; MIB change=%s; persistence change=%s\n' "${SETUP_PATCH_REQUIRED}" "${SU_MIB_CHANGE}" "${SU_PERSIST_CHANGE}"
    if [ "${SETUP_PATCH_REQUIRED}" = 1 ] && ! setup_patch_authorized; then
        setup_patch_recommendation
    fi
    setup_check_value 8311_gpon_sn "${CFG_ONU_SERIAL:-}"
    setup_check_value 8311_omcc_version "${CFG_ONU_OMCC_VERSION:-}"
    setup_check_value 8311_vendor_id "${CFG_ONU_VENDOR_ID:-}"
    setup_check_value 8311_equipment_id "${CFG_ONU_EQUIPMENT_ID:-}"
    setup_check_value 8311_hw_ver "${CFG_ONU_HARDWARE_VERSION:-}"
    setup_check_value 8311_ipaddr "${CFG_MANAGEMENT_IP_ADDRESS:-}"
    setup_check_value 8311_netmask "${CFG_MANAGEMENT_NETMASK:-}"
    setup_check_value 8311_gateway "${CFG_MANAGEMENT_GATEWAY:-}"
    setup_check_value 8311_ping_ip "${CFG_MANAGEMENT_PING_IP:-}"
    setup_check_value 8311_failsafe_delay "${CFG_MANAGEMENT_FAILSAFE_DELAY:-}"
    setup_check_value 8311_fix_vlans 2
    if [ "${SU_PROFILE}" != preserve ]; then setup_check_value mib_file "${SU_MIB}"; fi
    if [ "${SU_PROFILE}" = native_1u ]; then setup_check_value 8311_mib_file "${SU_MIB}"; fi
    if [ "${SU_PERSIST_CHANGE}" = 1 ]; then
        setup_check_value 8311_persist_root 1
        setup_check_value bootcmd "${SU_BOOT_PERSIST}"
    fi
    [ "${SU_MAINTENANCE}" = 0 ] || echo 'Setup: an offline maintenance phase is required before completing changes.'
    [ ! -e "${SU_FS}" ] || echo 'Setup: firmware failsafe is present; ownership will be preserved.'
    echo 'Setup check made no configuration changes.'
}
setup_patch_authorized() {
    [ "${CFG_LIBRARY_PATCH_ENABLED:-false}" = true ] &&
        [ "${CFG_LIBRARY_PATCH_POLICY:-auto}" = auto ]
}
setup_patch_recommendation() {
    echo 'Library patching requires explicit opt-in: set enabled = true in [library_patch], then rerun installation.'
    if [ "${CFG_LIBRARY_PATCH_POLICY:-auto}" != auto ]; then
        echo 'Also set policy = "auto" in [library_patch]; policy = "never" forbids binary patching.'
    fi
}
setup_check_value() {
    if [ -n "$2" ] && ! setup_env_matches "$1" "$2"; then
        printf 'Setup would update %s (value hidden).\n' "$1"
    fi
    return 0
}
setup_check_snapshot_reads() {
    [ ! -e "${SU_BACKUP:?setup paths required}" ] || return 0
    for su_var in ${SU_VARS:?setup paths required}; do
        setup_read_env_exact "${su_var}" "$1" || {
            setup_error "Cannot capture the original state of ${su_var}"
            return 1
        }
    done
}
setup_backup() {
    [ ! -L "${SU_STATE}" ] && [ ! -L "${SU_BACKUP}" ] || {
        setup_error 'Setup backup paths must not be symlinks'
        return 1
    }
    if [ -d "${SU_BACKUP}" ]; then
        if [ -L "${SU_BACKUP}/owner" ] || [ ! -f "${SU_BACKUP}/owner" ] || ! su_backup_owner=$(cat "${SU_BACKUP}/owner"); then
            setup_error 'Unknown setup backup; refusing to overwrite it'
            return 1
        fi
        case "${su_backup_owner}" in
            dei-setup-v1) [ ! -e "${SU_BACKUP}/applied-v1" ] && [ ! -L "${SU_BACKUP}/applied-v1" ] || return 1 ;;
            dei-setup-v2) [ -d "${SU_BACKUP}/applied-v1" ] && [ ! -L "${SU_BACKUP}/applied-v1" ] || return 1 ;;
            *)
                setup_error 'Unknown setup backup; refusing to overwrite it'
                return 1
                ;;
        esac
        for su_var in ${SU_VARS}; do
            [ ! -L "${SU_BACKUP}/${su_var}.state" ] && [ -f "${SU_BACKUP}/${su_var}.state" ] &&
                [ ! -L "${SU_BACKUP}/${su_var}.value" ] || return 1
            case "$(cat "${SU_BACKUP}/${su_var}.state" 2>/dev/null)" in
                present) [ -f "${SU_BACKUP}/${su_var}.value" ] || return 1 ;;
                absent) [ ! -e "${SU_BACKUP}/${su_var}.value" ] || return 1 ;;
                *)
                    setup_error 'Incomplete setup backup'
                    return 1
                    ;;
            esac
        done
        return 0
    fi
    (
        umask 077
        mkdir -p "${SU_STATE}"
    ) || return 1
    su_backup_temp=$(mktemp -d "${SU_STATE}/.fwenv-originals.XXXXXX") || return 1
    for su_var in ${SU_VARS}; do
        if ! setup_read_env_exact "${su_var}" "${su_backup_temp}/.read-error" ||
            ! printf '%s\n' "${SE_ENV_STATE}" >"${su_backup_temp}/${su_var}.state" ||
            { [ "${SE_ENV_STATE}" = present ] && ! printf '%s' "${SE_ENV_VALUE}" >"${su_backup_temp}/${su_var}.value"; }; then
            rm -rf "${su_backup_temp}"
            setup_error "Cannot capture the original state of ${su_var}"
            return 1
        fi
    done
    if ! rm "${su_backup_temp}/.read-error" || ! printf 'dei-setup-v2\n' >"${su_backup_temp}/owner" ||
        ! chmod 600 "${su_backup_temp}"/* || ! mkdir -m 700 "${su_backup_temp}/applied-v1"; then
        rm -rf "${su_backup_temp}"
        return 1
    fi
    mv "${su_backup_temp}" "${SU_BACKUP}" || {
        rm -rf "${su_backup_temp}"
        return 1
    }
}
setup_set() {
    if setup_env_matches "$1" "$2"; then return 0; fi
    # Older installations have no record of past writes. Preserve their backup
    # without claiming completeness retroactively. New installs record intent
    # before touching U-Boot, retaining the last verified value until readback.
    su_journal="${SU_BACKUP:?setup backup required}/applied-v1"
    if [ -e "${su_journal}" ] || [ -L "${su_journal}" ]; then
        [ -d "${su_journal}" ] && [ ! -L "${su_journal}" ] || return 1
        for su_record in "${su_journal}/$1" "${su_journal}/$1.pending"; do
            [ ! -L "${su_record}" ] && { [ ! -e "${su_record}" ] || [ -f "${su_record}" ]; } || return 1
        done
        setup_atomic_text "${su_journal}/$1.pending" "$2" || return 1
    fi
    fw_setenv "$1" "$2" >/dev/null 2>&1 || {
        setup_error "Cannot write $1"
        return 1
    }
    su_readback=$(fw_printenv -n "$1" 2>/dev/null) && [ "${su_readback}" = "$2" ] || {
        setup_error "Readback failed for $1"
        return 1
    }
    if [ -d "${su_journal}" ]; then
        mv -f "${su_journal}/$1.pending" "${su_journal}/$1" || return 1
    fi
    printf 'Updated %s (value hidden).\n' "$1"
    SETUP_REBOOT_REQUIRED=1
}
setup_optional() { [ -z "$2" ] || setup_set "$1" "$2"; }
setup_atomic_text() {
    su_text_temp=$(mktemp "$1.tmp.XXXXXX") || return 1
    if ! printf '%s\n' "$2" >"${su_text_temp}" || ! chmod 600 "${su_text_temp}" || ! mv -f "${su_text_temp}" "$1"; then
        rm -f "${su_text_temp}"
        return 1
    fi
}
setup_failsafe_on() {
    [ ! -L "${SU_FS}" ] && [ ! -L "${SU_FS_OWNER}" ] || {
        setup_error 'Failsafe ownership paths must not be symlinks'
        return 1
    }
    if [ -e "${SU_FS}" ]; then
        [ -f "${SU_FS}" ] || return 1
        return 0
    fi
    su_token_file=$(mktemp "${SU_STATE}/.failsafe-token.XXXXXX") || return 1
    su_token="dei-setup-failsafe-v1:${SU_BOOT}:${su_token_file##*/}"
    if ! printf '%s\n' "${su_token}" >"${su_token_file}" || ! mv -f "${su_token_file}" "${SU_FS_OWNER}"; then
        rm -f "${su_token_file}"
        return 1
    fi
    # The firmware tests existence only. Matching tokens survive device-number
    # changes across boots, unlike a recorded st_dev/inode pair.
    if ! (
        umask 077
        set -C
        printf '%s\n' "${su_token}" >"${SU_FS}"
    ); then
        rm -f "${SU_FS_OWNER}"
        setup_error 'Failsafe appeared while preparing maintenance; it was not overwritten'
        return 1
    fi
    setup_atomic_text "${SU_PENDING}" "${SU_BOOT}" || return 1
    SETUP_REBOOT_REQUIRED=1
    echo 'Created an owned firmware failsafe for the next maintenance boot.'
}
setup_backup_mib() {
    su_mib_hash=$(sha "$1") || return 1
    su_mib_backup="${SU_STATE}/mib.${su_mib_hash}.original"
    [ ! -L "${su_mib_backup}" ] || return 1
    if [ -e "${su_mib_backup}" ]; then
        [ -f "${su_mib_backup}" ] && su_backup_hash=$(sha "${su_mib_backup}") &&
            [ "${su_backup_hash}" = "${su_mib_hash}" ]
        return $?
    fi
    # Publish only a complete verified backup. A failed attempt can be retried.
    su_backup_stage=$(mktemp "${SU_STATE}/.mib-original.XXXXXX") || return 1
    if ! cat "$1" >"${su_backup_stage}" || ! su_backup_hash=$(sha "${su_backup_stage}") ||
        [ "${su_backup_hash}" != "${su_mib_hash}" ] || ! mv "${su_backup_stage}" "${su_mib_backup}"; then
        rm -f "${su_backup_stage}"
        return 1
    fi
}
setup_install_mib() {
    [ "${SU_PROFILE}" = dei_0205 ] || return 0
    [ ! -L "${SU_ROOT}${SU_MIB}" ] || {
        setup_error 'Refusing to replace a symlinked MIB'
        return 1
    }
    su_mib_temp=$(mktemp "${D}/.dei-mib.XXXXXX") || return 1
    if ! setup_build_mib "${su_mib_temp}"; then
        rm -f "${su_mib_temp}"
        return 1
    fi
    [ ! -L "${SU_ROOT}${SU_MIB}" ] || {
        rm -f "${su_mib_temp}"
        setup_error 'Refusing to replace a symlinked MIB'
        return 1
    }
    if cmp -s "${su_mib_temp}" "${SU_ROOT}${SU_MIB}"; then
        rm -f "${su_mib_temp}"
        return 0
    fi
    if [ -f "${SU_ROOT}${SU_MIB}" ]; then
        if ! setup_backup_mib "${SU_ROOT}${SU_MIB}"; then
            rm -f "${su_mib_temp}"
            setup_error 'Cannot verify the original MIB backup; preserving the current MIB'
            return 1
        fi
    fi
    if ! chmod 600 "${su_mib_temp}" || ! mv -f "${su_mib_temp}" "${SU_ROOT}${SU_MIB}"; then
        rm -f "${su_mib_temp}"
        return 1
    fi
    SETUP_REBOOT_REQUIRED=1
    echo 'Installed the verified DEI MIB; any previous file was retained.'
}
setup_apply() {
    setup_plan || return 1
    if [ "${SETUP_PATCH_REQUIRED}" = 1 ] && [ "${SU_LIBRARY_HASH}" != "${SU_PATCH_HASH}" ] && ! setup_patch_authorized; then
        setup_error 'The selected UNI requires a library patch, but binary patching is not explicitly authorized.'
        setup_patch_recommendation >&2
        return 1
    fi
    setup_backup || return 1
    setup_optional 8311_gpon_sn "${CFG_ONU_SERIAL:-}" &&
        setup_optional 8311_omcc_version "${CFG_ONU_OMCC_VERSION:-}" &&
        setup_optional 8311_vendor_id "${CFG_ONU_VENDOR_ID:-}" &&
        setup_optional 8311_equipment_id "${CFG_ONU_EQUIPMENT_ID:-}" &&
        setup_optional 8311_hw_ver "${CFG_ONU_HARDWARE_VERSION:-}" &&
        setup_optional 8311_ipaddr "${CFG_MANAGEMENT_IP_ADDRESS:-}" &&
        setup_optional 8311_netmask "${CFG_MANAGEMENT_NETMASK:-}" &&
        setup_optional 8311_gateway "${CFG_MANAGEMENT_GATEWAY:-}" &&
        setup_optional 8311_ping_ip "${CFG_MANAGEMENT_PING_IP:-}" &&
        setup_optional 8311_failsafe_delay "${CFG_MANAGEMENT_FAILSAFE_DELAY:-}" &&
        setup_set 8311_fix_vlans 2 || return 1
    if [ "${SU_PERSIST_CHANGE}" = 1 ]; then
        setup_set 8311_persist_root 1 && setup_set bootcmd "${SU_BOOT_PERSIST}" || return 1
    fi
    if [ "${SU_MAINTENANCE}" = 1 ]; then setup_failsafe_on || return 1; fi
    if [ "${SETUP_PATCH_REQUIRED}" = 1 ] || [ "${SU_PERSIST_CHANGE}" = 1 ] || [ -f "${SU_PERSIST_PENDING}" ]; then
        if ! setup_persistence_proven; then
            if [ ! -f "${SU_PERSIST_PENDING}" ]; then
                setup_atomic_text "${SU_PERSIST}" "${SU_BOOT}" && setup_atomic_text "${SU_PERSIST_PENDING}" "${SU_BOOT}" || return 1
            fi
            SETUP_REBOOT_REQUIRED=1
            echo 'Reboot required to prove the root overlay survives; run install again afterward.'
            return 0
        fi
    fi
    if [ "${SU_MAINTENANCE}" = 1 ]; then
        su_pending_boot=
        if [ -f "${SU_PENDING}" ]; then
            su_pending_boot=$(cat "${SU_PENDING}") && [ -n "${su_pending_boot}" ] || return 1
        fi
        if setup_running || [ "${su_pending_boot}" = "${SU_BOOT}" ]; then
            SETUP_REBOOT_REQUIRED=1
            echo 'Reboot required into firmware failsafe; run install again before restoring PON service.'
            return 0
        fi
        # The caller reads this setup result after the function returns.
        # shellcheck disable=SC2034
        SETUP_MAINTENANCE_READY=1
    fi
    setup_install_mib || return 1
    if [ "${SU_PROFILE}" = native_1u ]; then setup_set 8311_mib_file "${SU_MIB}" || return 1; fi
    if [ "${SU_PROFILE}" != preserve ]; then setup_set mib_file "${SU_MIB}" || return 1; fi
    SETUP_CONFIG_READY=1
    echo 'Setup configuration is ready; no process was restarted.'
}
setup_finish() {
    # Call only after binary handling and all payload installs succeeded.
    [ "${SETUP_CONFIG_READY:-0}" = 1 ] || return 0
    if [ "${SETUP_PATCH_REQUIRED:-0}" = 1 ] && ! setup_md5_matches "${SU_LIB}" "${SU_PATCH_HASH}"; then
        setup_error 'The required binary patch is not complete; failsafe remains in place'
        return 1
    fi
    if [ -f "${SU_PERSIST_PENDING}" ]; then
        setup_persistence_proven || {
            setup_error 'Persistence has not been proven; maintenance state is retained'
            return 1
        }
        rm -f "${SU_PERSIST_PENDING}" || return 1
    fi
    if [ -f "${SU_FS_OWNER}" ]; then
        su_owned_token=$(cat "${SU_FS_OWNER}") || return 1
        case "${su_owned_token}" in dei-setup-failsafe-v1:*) ;; *)
            setup_error 'Unknown failsafe ownership marker; preserving it'
            return 1
            ;;
        esac
        [ -f "${SU_FS}" ] && [ ! -L "${SU_FS}" ] && [ ! -L "${SU_FS_OWNER}" ] &&
            su_failsafe_token=$(cat "${SU_FS}") && [ "${su_owned_token}" = "${su_failsafe_token}" ] || {
            setup_error 'Failsafe ownership changed; preserving it'
            return 1
        }
        rm -f "${SU_FS}" "${SU_FS_OWNER}" "${SU_PENDING}" || return 1
        # The caller reads this setup result after the function returns.
        # shellcheck disable=SC2034
        SETUP_REBOOT_REQUIRED=1
        echo 'Removed only the installer-owned failsafe; reboot when ready to start the new configuration.'
    elif [ -e "${SU_FS}" ]; then
        echo 'Preserved your existing failsafe; PON startup remains disabled until you remove it.'
    fi
}
