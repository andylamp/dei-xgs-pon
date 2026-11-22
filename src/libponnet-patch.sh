#!/bin/sh
# Optional, exact-build UNI-0x0205 compatibility patch. Sourced by installer.
# A configured MIB is not proof of the currently loaded configuration.
LP_ROOT=${DEI835_MODULE_ROOT:-}
LP_LIB="${LP_ROOT}/usr/lib/libponnet.so.0.0.0"
LP_STOCK=5326f83e22a18929b830adfb82cfc483
LP_PATCHED=5ddb892fbf0b648479f605d9fddda50c
LP_BACKUP="${D:?installer configuration directory is required}/dei835-libponnet.stock"
LP_MARKER="${D}/dei835-libponnet.owner"
LP_OWNER="dei835-v2 ${LP_STOCK} ${LP_PATCHED}"

lp_env() {
    # Unset and unreadable selections both have no verified value.
    lp_env_value=$(fw_printenv -n "$1" 2>/dev/null) || return 0
    printf '%s\n' "${lp_env_value}"
}

lp_running() {
    for lp_comm in "${LP_ROOT}"/proc/[0-9]*/comm; do
        [ -f "${lp_comm}" ] || continue
        lp_name=$(cat "${lp_comm}" 2>/dev/null)
        if [ "${lp_name}" = omcid ] || [ "${lp_name}" = omcid.sh ]; then return 0; fi
        if [ -r "${lp_comm%/comm}/cmdline" ]; then
            tr '\000' ' ' <"${lp_comm%/comm}/cmdline" | grep -Eq '(^|[ /])omcid\.sh([[:space:]]|$)' && return 0
        fi
    done
    return 1
}

lp_storage() {
    LP_STORAGE_BLOCK=
    lp_persist_root=$(lp_env 8311_persist_root)
    lp_boot_command=$(lp_env bootcmd)
    [ "${lp_persist_root}" = 1 ] || LP_STORAGE_BLOCK="8311_persist_root is not 1"
    [ "${lp_boot_command}" = 'run ubi_init; run flash_flash' ] || LP_STORAGE_BLOCK="bootcmd is not the guide's persistent-overlay command"
    if ! awk '
        {mnt[NR]=$2; fs[NR]=$3; opts[NR]=$4}
        $2=="/" && ($3=="overlay" || $3=="overlayfs") && $4 ~ /(^|,)rw(,|$)/ {
            root=1; n=split($4, a, ",")
            for(i=1;i<=n;i++) if(a[i] ~ /^upperdir=/) {upper=a[i]; sub(/^upperdir=/,"",upper)}
        }
        END {
            if(!root || upper !~ /^\/overlay(\/|$)/) exit 1
            for(i=1;i<=NR;i++) {
                if((upper==mnt[i] || index(upper,mnt[i] "/")==1) && length(mnt[i])>best) {best=length(mnt[i]); backing=i}
                if(mnt[i]!="/" && ("/usr/lib/libponnet.so.0.0.0"==mnt[i] || index("/usr/lib/libponnet.so.0.0.0",mnt[i] "/")==1)) submount=1
            }
            if(!backing || fs[backing]!="ubifs" || opts[backing] !~ /(^|,)rw(,|$)/ || submount) exit 1
        }' "${LP_ROOT}/proc/mounts" 2>/dev/null; then
        LP_STORAGE_BLOCK="a writable root overlay with a persistent UBIFS upper layer is not verified"
    fi
    [ -z "${LP_STORAGE_BLOCK}" ]
}

lp_safety() {
    LP_BLOCK=
    [ -f "${LP_ROOT}/ptconf/.failsafe" ] || LP_BLOCK="firmware failsafe marker is absent"
    lp_running && LP_BLOCK="omcid or its startup script is running"
    lp_storage || LP_BLOCK=${LP_STORAGE_BLOCK}
    [ ! -L "${LP_LIB}" ] || LP_BLOCK="the target library is a symlink, not the expected regular file"
    [ -z "${LP_BLOCK}" ]
}

lp_resolve_module_path() {
    LP_RESOLVED_PATH=
    case "$1" in /*) ;; *) return 1 ;; esac
    lp_path_controls=$(printf '%s' "$1" | LC_ALL=C tr -cd '\000-\037\177' | wc -c)
    [ "${lp_path_controls}" -eq 0 ] 2>/dev/null || return 1
    LP_MODULE_SCOPE=$(readlink -f "${LP_ROOT:-/}" 2>/dev/null) || return 1
    # A nonempty root is an isolated module filesystem, never the host root.
    [ -z "${LP_ROOT}" ] || [ "${LP_MODULE_SCOPE}" != / ] || return 1
    lp_resolved_candidate=$(readlink -f "${LP_ROOT}$1" 2>/dev/null) || return 1
    case "${LP_MODULE_SCOPE}" in
        /) ;;
        *) case "${lp_resolved_candidate}" in "${LP_MODULE_SCOPE}"/*) ;; *) return 1 ;; esac ;;
    esac
    lp_path_controls=$(printf '%s' "${lp_resolved_candidate}" | LC_ALL=C tr -cd '\000-\037\177' | wc -c)
    [ "${lp_path_controls}" -eq 0 ] 2>/dev/null || return 1
    LP_RESOLVED_PATH=${lp_resolved_candidate}
}

lp_resolve_mib() {
    LP_MIB=
    LP_MIB_FILE=
    LP_MIB_STATUS=not-selected
    lp_mib_selection=$(lp_env mib_file)
    [ -n "${lp_mib_selection}" ] || return 0
    LP_MIB_STATUS=invalid-path
    lp_resolve_module_path "${lp_mib_selection}" || return 0
    LP_MIB_STATUS=unreadable
    [ -f "${LP_RESOLVED_PATH}" ] && [ -r "${LP_RESOLVED_PATH}" ] || return 0
    LP_MIB_FILE=${LP_RESOLVED_PATH}
    if [ "${LP_MODULE_SCOPE}" = / ]; then LP_MIB=${LP_MIB_FILE}; else LP_MIB=${LP_MIB_FILE#"${LP_MODULE_SCOPE}"}; fi
    LP_MIB_STATUS=ready
}

lp_log_path_allowed() {
    for lp_allowed_root in "${lp_log_tmp}" "${lp_log_var}" "${lp_log_ptconf}"; do
        [ -n "${lp_allowed_root}" ] || continue
        case "$1" in "${lp_allowed_root}"/*) return 0 ;; *) continue ;; esac
    done
    return 1
}

lp_log_diagnostics() {
    LP_LOG_EVIDENCE=none
    LP_LOG_FILES=0
    LP_LOG_MAPPING_MATCHES=0
    LP_LOG_UNI_MATCHES=0
    LP_LOG_RESET_MATCHES=0
    LP_LOG_LIMIT_REACHED=0
    # Read only bounded plaintext tails. Never replay source lines or assume
    # their timestamps establish freshness on a module with an unreliable clock.
    for lp_reader in tail head readlink find stat tr awk; do command -v "${lp_reader}" >/dev/null 2>&1 || return 0; done
    lp_log_tmp=
    lp_log_var=
    lp_log_ptconf=
    lp_resolve_module_path /tmp && lp_log_tmp=${LP_RESOLVED_PATH}
    lp_resolve_module_path /var && lp_log_var=${LP_RESOLVED_PATH}
    lp_resolve_module_path /ptconf && lp_log_ptconf=${LP_RESOLVED_PATH}
    lp_log_candidates=$(
        # Give the usual active filenames priority over older matching files.
        printf '%s\n' "${lp_log_tmp}/omcid.log" "${lp_log_var}/log/omcid.log"
        for lp_log_base in "${lp_log_tmp}" "${lp_log_var}" "${lp_log_ptconf}"; do
            [ -n "${lp_log_base}" ] && [ -d "${lp_log_base}" ] || continue
            lp_root_candidates=$(find "${lp_log_base}" -maxdepth 4 \( -type f -o -type l \) -iname '*omci*log*' -print 2>/dev/null | head -n 33)
            [ -n "${lp_root_candidates}" ] || continue
            lp_root_count=$(printf '%s\n' "${lp_root_candidates}" | wc -l)
            [ "${lp_root_count}" -le 32 ] || printf '%s\n' DEI-LOG-DISCOVERY-LIMIT
            printf '%s\n' "${lp_root_candidates}" | head -n 32
        done
    )
    lp_log_seen=' '
    while IFS= read -r lp_log_candidate; do
        [ -n "${lp_log_candidate}" ] || continue
        if [ "${lp_log_candidate}" = DEI-LOG-DISCOVERY-LIMIT ]; then
            LP_LOG_LIMIT_REACHED=1
            continue
        fi
        lp_log_path=$(readlink -f "${lp_log_candidate}" 2>/dev/null) || continue
        lp_log_path_allowed "${lp_log_path}" || continue
        [ -f "${lp_log_path}" ] && [ -r "${lp_log_path}" ] || continue
        case "${lp_log_path}" in *.gz | *.bz2 | *.xz | *.zip | *.zst | *.7z) continue ;; *) ;; esac
        lp_log_identity=$(stat -c '%d:%i' "${lp_log_path}" 2>/dev/null) || continue
        case "${lp_log_identity}" in '' | *[!0-9:]*) continue ;; *) ;; esac
        case "${lp_log_seen}" in *" ${lp_log_identity} "*) continue ;; *) ;; esac
        if [ "${LP_LOG_FILES}" -ge 32 ]; then
            LP_LOG_LIMIT_REACHED=1
            break
        fi
        # Reject binary/control-byte data before awk can truncate it at NUL.
        lp_log_controls=$(tail -c 262144 "${lp_log_path}" 2>/dev/null | head -c 262144 | LC_ALL=C tr -cd '\000-\010\013\014\016-\037\177' | wc -c)
        [ "${lp_log_controls}" -eq 0 ] 2>/dev/null || continue
        lp_log_counts=$(tail -c 262144 "${lp_log_path}" 2>/dev/null | head -c 262144 | tail -n 1024 | awk \
            -v creation="Can't create Managed Entity \"PPTP Ethernet UNI\"" '
            /pon_net_ifname_get\(\)[[:space:]]+failed[[:space:]]+with[[:space:]]+-12([^0-9]|$)/ {mapping++}
            /MIB[[:space:]]+reset[[:space:]]+failed/ {reset++}
            {
                start=index($0,creation)
                if(start && substr($0,start+length(creation)) ~ /^[[:space:]]+11@517([^0-9]|$)/) uni++
            }
            END {printf "%d %d %d\n", mapping, uni, reset}')
        case "${lp_log_counts}" in '' | *[!0-9\ ]*) continue ;; *) ;; esac
        IFS=' ' read -r lp_log_mapping lp_log_uni lp_log_reset <<EOF
${lp_log_counts}
EOF
        [ -n "${lp_log_mapping}" ] && [ -n "${lp_log_uni}" ] && [ -n "${lp_log_reset}" ] || continue
        lp_log_seen="${lp_log_seen}${lp_log_identity} "
        LP_LOG_FILES=$((LP_LOG_FILES + 1))
        LP_LOG_MAPPING_MATCHES=$((LP_LOG_MAPPING_MATCHES + lp_log_mapping))
        LP_LOG_UNI_MATCHES=$((LP_LOG_UNI_MATCHES + lp_log_uni))
        LP_LOG_RESET_MATCHES=$((LP_LOG_RESET_MATCHES + lp_log_reset))
    done <<EOF
${lp_log_candidates}
EOF
    # Reset failures and mapping errors alone never identify the affected UNI.
    [ "${LP_LOG_UNI_MATCHES}" -eq 0 ] || LP_LOG_EVIDENCE=uni-mapping
    return 0
}

lp_find_pon_command() {
    LP_PON_COMMAND=
    lp_pon_scope=$(readlink -f "${LP_ROOT:-/}" 2>/dev/null) || return 1
    [ -z "${LP_ROOT}" ] || [ "${lp_pon_scope}" != / ] || return 1
    lp_pon_search=${PATH:-}
    while [ -n "${lp_pon_search}" ]; do
        lp_pon_directory=${lp_pon_search%%:*}
        if [ -n "${lp_pon_directory}" ] && [ -f "${lp_pon_directory}/pon" ] && [ -x "${lp_pon_directory}/pon" ]; then
            lp_pon_candidate=$(readlink -f "${lp_pon_directory}/pon" 2>/dev/null) || lp_pon_candidate=
            if [ -n "${lp_pon_candidate}" ]; then
                case "${lp_pon_scope}" in
                    /) LP_PON_COMMAND="${lp_pon_directory}/pon" ;;
                    *) case "${lp_pon_candidate}" in "${lp_pon_scope}"/*) LP_PON_COMMAND="${lp_pon_directory}/pon" ;; *) ;; esac ;;
                esac
                [ -z "${LP_PON_COMMAND}" ] || return 0
            fi
        fi
        case "${lp_pon_search}" in *:*) lp_pon_search=${lp_pon_search#*:} ;; *) break ;; esac
    done
    return 1
}

lp_pon_context() {
    LP_PON_CONTEXT=unavailable
    LP_PON_CURRENT=unavailable
    LP_PON_PREVIOUS=unavailable
    LP_PON_TIME_CURR=unavailable
    lp_pon_timeout=${DEI835_PON_TIMEOUT_SECONDS:-5}
    case "${lp_pon_timeout}" in [1-9] | [12][0-9] | 30) ;; *) return 0 ;; esac
    command -v timeout >/dev/null 2>&1 && lp_find_pon_command || return 0
    # Bound this read-only vendor command, never the lock-owning omci_pipe.sh.
    lp_pon_reply=$(timeout -s KILL "${lp_pon_timeout}" "${LP_PON_COMMAND}" psg 7>&- 8>&- 9>&- 2>/dev/null) || return 0
    lp_pon_fields=$(printf '%s\n' "${lp_pon_reply}" | awk '
        {
            errors=0; currents=0; previouses=0; times=0; touched=0
            for(i=1;i<=NF;i++) {
                if($i ~ /^errorcode=/) {errors++; error=$i; touched=1}
                if($i ~ /^current=/) {currents++; current=$i; sub(/^current=/,"",current); touched=1}
                if($i ~ /^previous=/) {previouses++; previous=$i; sub(/^previous=/,"",previous); touched=1}
                if($i ~ /^time_curr=/) {times++; duration=$i; sub(/^time_curr=/,"",duration); touched=1}
            }
            if(touched) {
                if(errors!=1 || error!="errorcode=0" || currents!=1 || previouses!=1 || times!=1 ||
                   current !~ /^[0-9]+$/ || length(current)>3 || previous !~ /^[0-9]+$/ || length(previous)>3 ||
                   duration !~ /^[0-9]+$/ || length(duration)>10) bad=1
                else {records++; saved_current=current; saved_previous=previous; saved_duration=duration}
            }
        }
        END {if(!bad && records==1) printf "%d %d %.0f\n", saved_current, saved_previous, saved_duration}')
    [ -n "${lp_pon_fields}" ] || return 0
    IFS=' ' read -r LP_PON_CURRENT LP_PON_PREVIOUS LP_PON_TIME_CURR <<EOF
${lp_pon_fields}
EOF
    if [ "${LP_PON_CURRENT}" = 51 ]; then LP_PON_CONTEXT=O5; else LP_PON_CONTEXT=non-O5; fi
}

lp_recommendation() {
    lp_log_diagnostics
    lp_pon_context
    LP_RECOMMENDATION=not-indicated
    case "${LP_STATE}" in
        needed) LP_RECOMMENDATION=suggested ;;
        patched) ;;
        unknown | missing | incompatible) LP_RECOMMENDATION=unavailable ;;
        *)
            if [ "${LP_LOG_EVIDENCE}" = uni-mapping ]; then
                LP_RECOMMENDATION=suggested
            elif [ "${LP_STATE}" = unresolved ] && [ "${LP_LOG_FILES}" = 0 ]; then
                LP_RECOMMENDATION=unavailable
            fi
            ;;
    esac
    echo "PON context: ${LP_PON_CONTEXT} current=${LP_PON_CURRENT} previous=${LP_PON_PREVIOUS} time_curr=${LP_PON_TIME_CURR}."
    echo 'O5 alone does not establish a need for this patch; time_curr is the age of the current PON state.'
    if [ "${LP_LOG_FILES}" -eq 0 ]; then
        echo 'OMCI log diagnostics unavailable: no readable supported module log was found.'
    else
        echo "OMCI log diagnostics: files=${LP_LOG_FILES}; mapping-errors=${LP_LOG_MAPPING_MATCHES}; UNI-517-creation-errors=${LP_LOG_UNI_MATCHES}; MIB-reset-errors=${LP_LOG_RESET_MATCHES}."
        echo 'Only bounded log tails were inspected; entries may be historical and are not proof of a current fault.'
        echo 'Search: /tmp, /var, /ptconf; depth <= 4; <= 32 distinct plaintext logs; last 256 KiB and 1024 lines per file.'
        [ "${LP_LOG_LIMIT_REACHED}" = 0 ] || echo 'OMCI log file limit reached; discovery may be incomplete.'
    fi
    echo "Binary patch recommendation: ${LP_RECOMMENDATION}."
    if [ "${LP_RECOMMENDATION}" = suggested ]; then
        if [ "${LP_STATE}" != needed ]; then
            echo 'The specific UNI-0x0205 creation failure suggests a possible mapping issue, but the selected MIB does not establish eligibility.'
            echo 'If UNI 0x0205 matches your line, review [onu] mib_profile = "dei_0205" before considering the patch.'
        fi
        echo 'To opt in after review, set [library_patch] enabled = true with policy = "auto", then rerun check and install with that configuration.'
        echo 'Log evidence never replaces the exact-library, selected-MIB, failsafe, or persistent-storage checks.'
    elif [ "${LP_LOG_EVIDENCE}" = uni-mapping ]; then
        case "${LP_STATE}" in
            patched) echo 'The library is already patched; historical log matches do not justify applying it again.' ;;
            unknown | missing | incompatible) echo 'Specific log evidence exists, but this library/MIB combination cannot authorize the known patch.' ;;
            *) ;;
        esac
    fi
}

lp_assess() {
    LP_STATE=unknown
    LP_HASH=$(md5 "${LP_LIB}") || LP_HASH=
    lp_resolve_mib
    LP_UNI=unresolved
    # Only the explicit native override is known to select a custom MIB here.
    # Do not guess the launcher's default from 8311_mib_file or a leftover file.
    if [ "${LP_MIB_STATUS}" = ready ]; then
        LP_UNI=$(awk '
                    /^[[:space:]]*#/ {next}
                    {
                        n=1
                        if($1=="!" || $1=="?") n=2
                        if($n=="11") {
                            count++; if(n==2) conditional++; id=tolower($(n+1))
                            if(id=="0x0205" || id=="0x205" || id=="517") target++
                            if(id=="0x0101" || id=="0x101" || id=="257") native++
                        }
                    }
                    END {
                        if(count>1) print "multiple-UNIs"
                        else if(conditional) print "conditional-UNI"
                        else if(count==1 && target==1) print "single-0x0205"
                        else if(count==1 && native==1) print "single-0x0101"
                        else if(count==1) print "other-UNI"
                        else print "no-Ethernet-UNI"
                    }
                    ' "${LP_MIB_FILE}")
    fi
    case "${LP_HASH}" in
        "${LP_PATCHED}")
            if [ "${LP_UNI}" = single-0x0101 ]; then LP_STATE=incompatible; else LP_STATE=patched; fi
            ;;
        "${LP_STOCK}")
            case "${LP_UNI}" in
                single-0x0205) LP_STATE=needed ;;
                single-0x0101 | other-UNI | no-Ethernet-UNI) LP_STATE=not-applicable ;;
                *) LP_STATE=unresolved ;;
            esac
            ;;
        '') LP_STATE=missing ;;
        *) LP_STATE=unknown ;;
    esac
    echo "libponnet: ${LP_STATE} (md5=${LP_HASH:-unreadable})"
    if [ "${LP_MIB_STATUS}" = ready ]; then
        printf 'Configured native MIB: %s; UNI layout: %s\n' "${LP_MIB}" "${LP_UNI}"
    else
        echo "Configured native MIB: unavailable (${LP_MIB_STATUS}); UNI layout: ${LP_UNI}"
    fi
    case "${LP_STATE}" in
        patched)
            echo "Binary decision: already patched; leave unchanged."
            [ "${LP_UNI}" = single-0x0205 ] || echo 'WARNING: the patched library is present, but compatibility with the selected MIB is unverified.'
            ;;
        incompatible)
            echo 'WARNING: the known UNI-0x0205 patched library is incompatible with the selected native UNI 0x0101.'
            echo 'Review the MIB/library combination before the next boot; no automatic restoration or further patch is permitted.'
            ;;
        not-applicable) echo "Binary decision: patch does not apply to the selected UNI layout; skip." ;;
        unresolved) echo "Binary decision: applicability is not established; skip (never infer from O5 or old logs)." ;;
        needed)
            echo "Binary decision: known stock library is incompatible with the configured sole UNI 0x0205."
            echo "This describes the next-start configuration, not proof that the running daemon loaded it."
            if lp_safety; then
                echo "Binary application: maintenance prerequisites satisfied."
            else
                echo "Binary application deferred: ${LP_BLOCK}."
            fi
            ;;
        *) echo "Binary decision: unsupported/unreadable library; refuse binary patching." ;;
    esac
    if [ "${LP_HASH}" = "${LP_PATCHED}" ]; then
        if lp_storage; then
            echo "Binary persistence: persistent writable upper layer and boot settings verified."
        else
            echo "WARNING: binary persistence is unverified: ${LP_STORAGE_BLOCK}."
        fi
    fi
    lp_recommendation
}

lp_lock_worker() {
    mkdir -p "${R:?installer run directory is required}" || return 1
    : >"${R}/stop" || return 1
    exec 7>"${R}/worker.lock"
    lp_tries=0
    while ! flock -n 7; do
        [ "${lp_tries}" -lt 40 ] || {
            echo "ERROR: worker still finishing a vendor call; no binary changes" >&2
            return 1
        }
        sleep 1
        lp_tries=$((lp_tries + 1))
    done
}

lp_unlock_worker() {
    flock -u 7
    exec 7>&-
}

lp_owner_valid() {
    [ -f "${LP_MARKER}" ] || return 1
    lp_marker_value=$(cat "${LP_MARKER}") || return 1
    [ "${lp_marker_value}" = "${LP_OWNER}" ]
}

lp_hash_matches() {
    lp_actual_hash=$(md5 "$1") || return 1
    [ -n "$2" ] && [ "${lp_actual_hash}" = "$2" ]
}

lp_backup_check() {
    if [ -L "${LP_BACKUP}" ] || [ -L "${LP_MARKER}" ]; then
        echo "ERROR: libponnet backup/ownership paths must not be symlinks" >&2
        return 1
    fi
    if [ -e "${LP_BACKUP}" ] && ! lp_hash_matches "${LP_BACKUP}" "${LP_STOCK}"; then
        echo "ERROR: existing libponnet backup is not the known stock file; preserve it" >&2
        return 1
    fi
    if [ -e "${LP_MARKER}" ] && ! lp_owner_valid; then
        echo "ERROR: unknown libponnet ownership marker; no binary changes" >&2
        return 1
    fi
}

lp_patch_opted_in() {
    if [ "${CFG_LIBRARY_PATCH_ENABLED:-false}" != true ] || [ "${CFG_LIBRARY_PATCH_POLICY:-}" != auto ]; then
        echo 'ERROR: binary patch requires explicit [library_patch] enabled = true and policy = "auto".' >&2
        echo 'Review check output, update the configuration, and rerun install --config PATH.' >&2
        return 1
    fi
    return 0
}

lp_patch() {
    LP_APPLIED=0
    # Assessment is refreshed immediately before the operation.
    lp_assess
    case "${LP_STATE}" in
        patched | not-applicable | unresolved) return 0 ;;
        needed) ;;
        *) return 1 ;;
    esac
    lp_patch_opted_in || return 1
    lp_safety || {
        echo "ERROR: binary patch deferred: ${LP_BLOCK}" >&2
        return 1
    }
    lp_backup_check || return 1
    for lp_cmd in cmp dd; do command -v "${lp_cmd}" >/dev/null 2>&1 || {
        echo "ERROR: missing ${lp_cmd}" >&2
        return 1
    }; done
    lp_patch_opted_in || return 1
    lp_lock_worker || return 1
    lp_stage=$(mktemp "${LP_LIB%/*}/.dei835-libponnet.XXXXXX") || {
        lp_unlock_worker
        return 1
    }
    if ! cp -p "${LP_LIB}" "${lp_stage}" || ! lp_hash_matches "${lp_stage}" "${LP_STOCK}"; then
        echo "ERROR: stock library changed or staging failed" >&2
        rm -f "${lp_stage}"
        lp_unlock_worker
        return 1
    fi
    if ! printf '\373' | dd of="${lp_stage}" bs=1 seek=236395 conv=notrunc 2>/dev/null; then
        rm -f "${lp_stage}"
        lp_unlock_worker
        return 1
    fi
    lp_difference=$(cmp -l "${LP_LIB}" "${lp_stage}" | awk '{print $1, $2, $3}')
    if ! lp_hash_matches "${lp_stage}" "${LP_PATCHED}" || [ "${lp_difference}" != '236396 377 373' ]; then
        echo "ERROR: patched checksum or single-byte verification failed; active library unchanged" >&2
        rm -f "${lp_stage}"
        lp_unlock_worker
        return 1
    fi
    if [ ! -e "${LP_BACKUP}" ]; then
        lp_saved=$(mktemp "${D}/.dei835-libponnet-stock.XXXXXX") || {
            rm -f "${lp_stage}"
            lp_unlock_worker
            return 1
        }
        if ! cp -p "${LP_LIB}" "${lp_saved}" || ! lp_hash_matches "${lp_saved}" "${LP_STOCK}" || ! mv "${lp_saved}" "${LP_BACKUP}"; then
            rm -f "${lp_saved}" "${lp_stage}"
            lp_unlock_worker
            return 1
        fi
    fi
    # Recheck safety, configuration and preimage immediately before committing.
    lp_assess
    if [ "${LP_STATE}" != needed ] || ! lp_patch_opted_in || ! lp_safety || ! lp_hash_matches "${LP_BACKUP}" "${LP_STOCK}"; then
        echo "ERROR: preconditions changed during staging; active library unchanged" >&2
        rm -f "${lp_stage}"
        lp_unlock_worker
        return 1
    fi
    lp_owner_tmp=$(mktemp "${D}/.dei835-libponnet-owner.XXXXXX") || {
        rm -f "${lp_stage}"
        lp_unlock_worker
        return 1
    }
    if ! printf '%s\n' "${LP_OWNER}" >"${lp_owner_tmp}" || ! mv "${lp_owner_tmp}" "${LP_MARKER}"; then
        rm -f "${lp_owner_tmp}" "${lp_stage}"
        lp_unlock_worker
        return 1
    fi
    # Same-filesystem rename; never overwrite an existing mapping in place.
    if ! mv -f "${lp_stage}" "${LP_LIB}"; then
        rm -f "${lp_stage}"
        lp_unlock_worker
        return 1
    fi
    lp_hash_matches "${LP_LIB}" "${LP_PATCHED}" || {
        echo "ERROR: final libponnet verification failed" >&2
        lp_unlock_worker
        return 1
    }
    lp_unlock_worker
    # The installer uses this result to record the operation and request a reboot.
    # shellcheck disable=SC2034
    LP_APPLIED=1
    echo "Applied and verified the one-byte UNI-0x0205 patch. Stock backup: ${LP_BACKUP}"
    echo "No process was restarted. Failsafe remains present; complete maintenance before a planned reboot."
}

lp_restore() {
    lp_backup_check || return 1
    lp_owner_valid || {
        echo "ERROR: this installer does not own the binary patch; no restoration" >&2
        return 1
    }
    lp_hash_matches "${LP_BACKUP}" "${LP_STOCK}" || {
        echo "ERROR: verified stock backup missing" >&2
        return 1
    }
    lp_current=$(md5 "${LP_LIB}") || return 1
    case "${lp_current}" in "${LP_STOCK}" | "${LP_PATCHED}") ;; *)
        echo "ERROR: current library is unknown; no restoration" >&2
        return 1
        ;;
    esac
    lp_safety || {
        echo "ERROR: binary restoration deferred: ${LP_BLOCK}" >&2
        return 1
    }
    lp_lock_worker || return 1
    lp_stage=$(mktemp "${LP_LIB%/*}/.dei835-libponnet.XXXXXX") || {
        lp_unlock_worker
        return 1
    }
    if ! cp -p "${LP_BACKUP}" "${lp_stage}" || ! lp_hash_matches "${lp_stage}" "${LP_STOCK}" || ! lp_safety || ! lp_hash_matches "${LP_LIB}" "${lp_current}"; then
        rm -f "${lp_stage}"
        lp_unlock_worker
        return 1
    fi
    if ! mv -f "${lp_stage}" "${LP_LIB}"; then
        rm -f "${lp_stage}"
        lp_unlock_worker
        return 1
    fi
    lp_hash_matches "${LP_LIB}" "${LP_STOCK}" || {
        echo "ERROR: restored library verification failed" >&2
        lp_unlock_worker
        return 1
    }
    rm -f "${LP_MARKER}"
    lp_unlock_worker
    echo "Restored the verified stock library; backup retained. No restart/reboot performed."
    echo "A selected UNI-0x0205 MIB remains incompatible with this stock library."
}
