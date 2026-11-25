#!/bin/sh
# Read-only inventory and repeated PON observations. Sourced by the installer.

module_inventory() {
    echo 'Current firmware selection (not proof of the MIB loaded by omcid):'
    printf '  mib_file: %s (status=%s; UNI=%s)\n' "${LP_MIB:-unavailable}" "${LP_MIB_STATUS:-unavailable}" "${LP_UNI:-unavailable}"
    md_mode=$(lp_env 8311_fix_vlans)
    case "${md_mode}" in 0 | 1 | 2) ;; '') md_mode='unset' ;; *) md_mode=unrecognized ;; esac
    md_omcc=$(lp_env 8311_omcc_version)
    case "${md_omcc}" in 0x8[2-9A-F] | 0x[9AB][0-9A-F]) ;; '') md_omcc='unset' ;; *) md_omcc=unrecognized ;; esac
    echo "  8311_fix_vlans=${md_mode}; 8311_omcc_version=${md_omcc}"
    echo 'Managed file inventory (contents and unrelated files omitted):'
    for md_name in dei-pon.toml dei.ini dei-config.sh dei-835-lib.sh dei-lct.sh keyring.sh status.sh vlan_fixes_hook.sh dei835.log install.log setup-state; do
        md_file="${D:?installer configuration directory is required}/${md_name}"
        if [ -L "${md_file}" ]; then
            echo "  ${md_name}: symlink (not followed)"
        elif [ -f "${md_file}" ] || [ -d "${md_file}" ]; then
            md_metadata=$(stat -c 'mode=%a bytes=%s' "${md_file}" 2>/dev/null) || md_metadata=unavailable
            if [ -d "${md_file}" ]; then md_type=directory; else md_type=regular; fi
            echo "  ${md_name}: ${md_type} ${md_metadata}"
        elif [ -e "${md_file}" ]; then
            echo "  ${md_name}: special (not read)"
        else
            echo "  ${md_name}: absent"
        fi
    done
}

module_boot_observe() {
    MD_UPTIME=unavailable
    MD_BOOT=unavailable
    if lp_resolve_module_path /proc/uptime && [ -f "${LP_RESOLVED_PATH:-}" ]; then
        MD_UPTIME=$(head -c 128 "${LP_RESOLVED_PATH}" | awk '
            NR==1 && $1 ~ /^[0-9]+([.][0-9]+)?$/ && length($1)<=20 {printf "%.0f", int($1)}')
        MD_UPTIME=${MD_UPTIME:-unavailable}
    fi
    if lp_resolve_module_path /proc/sys/kernel/random/boot_id && [ -f "${LP_RESOLVED_PATH:-}" ]; then
        md_boot_candidate=$(head -c 128 "${LP_RESOLVED_PATH}")
        case "${md_boot_candidate}" in
            '' | *[!0-9a-f-]*) ;;
            *) [ "${#md_boot_candidate}" != 36 ] || MD_BOOT=${md_boot_candidate} ;;
        esac
    fi
}

module_pon_observe() {
    md_samples=$1
    md_interval=$2
    md_index=1
    md_missing=0
    md_non_o5=0
    md_changes=0
    md_resets=0
    md_advances=0
    md_reboots=0
    md_last_current=unavailable
    md_last_age=unavailable
    md_last_boot=unavailable
    md_last_uptime=unavailable
    md_gem_started=0
    echo "PON observation: ${md_samples} samples, ${md_interval}s between samples; no firmware hook lock held."
    while [ "${md_index}" -le "${md_samples}" ]; do
        lp_pon_context
        module_boot_observe
        echo "Sample ${md_index}: current=${LP_PON_CURRENT:-unavailable} previous=${LP_PON_PREVIOUS:-unavailable} time_curr=${LP_PON_TIME_CURR:-unavailable} uptime=${MD_UPTIME} context=${LP_PON_CONTEXT:-unavailable}"
        if [ "${MD_BOOT}" != unavailable ] && [ "${md_last_boot}" != unavailable ] && [ "${MD_BOOT}" != "${md_last_boot}" ]; then
            md_reboots=$((md_reboots + 1))
        elif [ "${MD_UPTIME}" != unavailable ] && [ "${md_last_uptime}" != unavailable ] && awk -v a="${MD_UPTIME}" -v b="${md_last_uptime}" 'BEGIN {exit !(a<b)}'; then
            md_reboots=$((md_reboots + 1))
        fi
        if [ "${LP_PON_CONTEXT:-unavailable}" = unavailable ]; then
            md_missing=$((md_missing + 1))
        else
            [ "${LP_PON_CONTEXT:-unavailable}" = O5 ] || md_non_o5=$((md_non_o5 + 1))
            if [ "${md_last_current}" != unavailable ]; then
                if [ "${LP_PON_CURRENT:-unavailable}" != "${md_last_current}" ]; then
                    md_changes=$((md_changes + 1))
                elif awk -v a="${LP_PON_TIME_CURR:-unavailable}" -v b="${md_last_age}" 'BEGIN {exit !(a<b)}'; then
                    md_resets=$((md_resets + 1))
                elif awk -v a="${LP_PON_TIME_CURR:-unavailable}" -v b="${md_last_age}" 'BEGIN {exit !(a>b)}'; then
                    md_advances=$((md_advances + 1))
                fi
            fi
        fi
        md_last_current=${LP_PON_CURRENT:-unavailable}
        md_last_age=${LP_PON_TIME_CURR:-unavailable}
        md_last_boot=${MD_BOOT}
        md_last_uptime=${MD_UPTIME}
        if [ "${md_index}" = 1 ] && [ "${md_samples}" -gt 1 ] &&
            [ "${LIVE_GEMS_READY:-0}" = 1 ] && [ "${LP_PON_CONTEXT:-unavailable}" = O5 ]; then
            if DEI_PON_CONF_DIR="${S:?installer staging directory required}" sh "${S}/gem-diagnostics.sh" first; then
                md_gem_started=1
            fi
        fi
        md_index=$((md_index + 1))
        if [ "${md_index}" -le "${md_samples}" ] && ! sleep "${md_interval}"; then
            echo 'PON observation interrupted: requested sample interval could not be completed.' >&2
            return 1
        fi
    done
    if [ "${md_samples}" = 1 ]; then
        echo 'GEM counter comparison needs at least two samples.'
    elif [ "${md_missing}" != 0 ] || [ "${md_non_o5}" != 0 ] || [ "${md_reboots}" != 0 ] ||
        [ "${md_resets}" != 0 ] || [ "${md_advances}" -ne $((md_samples - 1)) ]; then
        echo 'GEM counters inconclusive: continuous advancing O5 was not observed.'
    elif [ "${md_gem_started}" = 1 ]; then
        DEI_PON_CONF_DIR="${S}" sh "${S}/gem-diagnostics.sh" last || :
    elif [ "${LIVE_GEMS_READY:-0}" != 1 ]; then
        echo 'GEM counters unavailable: selected service discovery is incomplete.'
    fi
    echo "PON summary: unavailable=${md_missing} non-O5=${md_non_o5} state-changes=${md_changes} timer-resets=${md_resets} module-restarts=${md_reboots}."
    if [ "${md_changes}" -gt 0 ] || [ "${md_resets}" -gt 0 ] || [ "${md_reboots}" -gt 0 ]; then
        echo 'Continuity was interrupted: state transitions, reactivation/ranging, or a module restart may explain this; the samples do not establish an OLT fault.'
    elif [ "${md_samples}" -gt 1 ] && [ "${md_missing}" = 0 ] && [ "${md_non_o5}" = 0 ] && [ "${md_advances}" -eq $((md_samples - 1)) ]; then
        echo 'O5 with an advancing state age was observed throughout this sample window; this does not establish Internet connectivity.'
    else
        echo 'Stable O5 is not established by these samples (missing/non-O5 data, a single sample, or a nonadvancing timer).'
    fi
}
