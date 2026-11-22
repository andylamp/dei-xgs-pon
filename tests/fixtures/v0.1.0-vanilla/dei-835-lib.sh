#!/bin/sh
# DEI selected-service repair. No device identity, MIB or bridge-port changes.
DEI_DIR=${DEI835_CONF_DIR:-/ptconf/8311}
# Shared location for the watcher PID/lock and the status command.
# shellcheck disable=SC2034
DEI_RUN=${DEI835_RUN_DIR:-/tmp/dei835}
DEI_NET=${DEI835_NET_DIR:-/sys/class/net}

# Absent configuration is a legacy-v1 compatibility default, not a fallback
# for an explicitly selected missing or invalid configuration file.
dei_runtime_config() {
    DEI_VLAN=835
    DEI_ENCRYPTION=3
    DEI_NORMALIZE=true
    DEI_WATCH_ENABLED=true
    DEI_INTERVAL=10
    if [ "${DEI835_CONFIG_PATH+x}" = x ]; then
        dei_config_path=${DEI835_CONFIG_PATH}
        [ -n "${dei_config_path}" ] && [ -f "${dei_config_path}" ] && [ -r "${dei_config_path}" ] || {
            DEI_ERROR="explicit runtime configuration is missing or unreadable"
            return 1
        }
    else
        dei_config_path="${DEI_DIR}/dei-pon.toml"
        if [ ! -e "${dei_config_path}" ] && [ ! -L "${dei_config_path}" ]; then
            DEI_CONFIG_SIGNATURE="835:3:true:true:10"
            return 0
        fi
        [ -f "${dei_config_path}" ] && [ -r "${dei_config_path}" ] || {
            DEI_ERROR="stored runtime configuration is unreadable"
            return 1
        }
    fi
    [ -r "${DEI_DIR}/dei-config.sh" ] || {
        DEI_ERROR="configuration loader is missing"
        return 1
    }
    . "${DEI_DIR}/dei-config.sh" || {
        DEI_ERROR="configuration loader failed"
        return 1
    }
    dei_config_load "${dei_config_path}" || {
        DEI_ERROR="invalid runtime configuration; no changes"
        return 1
    }
    DEI_VLAN=${CFG_INTERNET_VLAN}
    DEI_ENCRYPTION=${CFG_INTERNET_ENCRYPTION_KEY_RING}
    DEI_NORMALIZE=${CFG_INTERNET_NORMALIZE_VLAN}
    DEI_WATCH_ENABLED=${CFG_WATCHER_ENABLED}
    DEI_INTERVAL=${CFG_WATCHER_INTERVAL_SECONDS}
    DEI_CONFIG_SIGNATURE="${DEI_VLAN}:${DEI_ENCRYPTION}:${DEI_NORMALIZE}:${DEI_WATCH_ENABLED}:${DEI_INTERVAL}"
}

dei_find_pon_command() {
    DEI_PON_COMMAND=
    dei_pon_root=${DEI835_MODULE_ROOT:-}
    dei_pon_scope=$(readlink -f "${dei_pon_root:-/}" 2>/dev/null) || return 1
    [ -z "${dei_pon_root}" ] || [ "${dei_pon_scope}" != / ] || return 1
    dei_pon_search=${PATH:-}
    while [ -n "${dei_pon_search}" ]; do
        dei_pon_directory=${dei_pon_search%%:*}
        if [ -n "${dei_pon_directory}" ] && [ -f "${dei_pon_directory}/pon" ] && [ -x "${dei_pon_directory}/pon" ]; then
            dei_pon_candidate=$(readlink -f "${dei_pon_directory}/pon" 2>/dev/null) || dei_pon_candidate=
            if [ -n "${dei_pon_candidate}" ]; then
                case "${dei_pon_candidate}" in
                    "${dei_pon_scope%/}"/*)
                        DEI_PON_COMMAND="${dei_pon_directory}/pon"
                        return 0
                        ;;
                    *) ;; # Keep searching PATH for a command inside the module.
                esac
            fi
        fi
        case "${dei_pon_search}" in *:*) dei_pon_search=${dei_pon_search#*:} ;; *) break ;; esac
    done
    return 1
}

dei_prepare_pon_read() {
    DEI_PON_READ_ERROR=
    dei_pon_timeout=${DEI835_PON_TIMEOUT_SECONDS:-5}
    case "${dei_pon_timeout}" in
        [1-9] | [12][0-9] | 30) ;;
        *)
            DEI_PON_READ_ERROR="invalid PON status timeout; no runtime changes"
            return 1
            ;;
    esac
    if ! command -v timeout >/dev/null 2>&1 || ! dei_find_pon_command; then
        DEI_PON_READ_ERROR="cannot locate a bounded module PON command; no runtime changes"
        return 1
    fi
}

dei_read_pon() {
    case "$#:${1:-}" in
        1:psg) ;;
        2:gpcg) case "$2" in '' | *[!0-9]*) return 1 ;; *) ;; esac ;;
        *) return 1 ;;
    esac
    dei_prepare_pon_read || {
        printf 'ERROR: %s\n' "${DEI_PON_READ_ERROR}" >&2
        return 1
    }
    # Only this read-only vendor command is bounded. BusyBox's watchdog may
    # outlive it briefly, so it must not inherit any installer or worker lock.
    timeout -s KILL "${dei_pon_timeout}" "${DEI_PON_COMMAND}" "$@" 7>&- 8>&- 9>&- 2>/dev/null
}

dei_ready() {
    DEI_READY_ERROR=
    dei_failsafe="${DEI835_MODULE_ROOT:-}/ptconf/.failsafe"
    if [ -e "${dei_failsafe}" ] || [ -L "${dei_failsafe}" ]; then
        DEI_READY_ERROR="firmware failsafe is present; no runtime changes"
        return 1
    fi
    dei_prepare_pon_read || {
        DEI_READY_ERROR=${DEI_PON_READ_ERROR}
        return 1
    }
    dei_pon=$(dei_read_pon psg) || {
        DEI_READY_ERROR="cannot read PON status within the bounded interval; no runtime changes"
        return 1
    }
    if printf '%s\n' "${dei_pon}" | awk '
        NF {
            records++
            for(i=1;i<=NF;i++) {
                if($i ~ /^errorcode=/) {errors++; if($i=="errorcode=0") ok=1}
                if($i ~ /^current=/) {states++; if($i=="current=51") ready=1}
            }
        }
        END {exit !(records==1 && errors==1 && states==1 && ok && ready)}'; then
        return 0
    fi
    DEI_READY_ERROR="waiting for PON O5"
    return 1
}

dei_log() {
    printf '%s\n' "$*"
    [ "${DEI_LOGGING:-0}" = 1 ] || return 0
    DEI_LOG_BUFFER="${DEI_LOG_BUFFER:-}$*
"
}

dei_log_ready() {
    for dei_log_path in "${DEI_DIR}/dei835.log" "${DEI_DIR}/dei835.log.old"; do
        if [ -L "${dei_log_path}" ]; then
            echo 'ERROR: runtime log paths must not be symlinks' >&2
            return 1
        fi
        if [ -e "${dei_log_path}" ] && [ ! -f "${dei_log_path}" ]; then
            echo 'ERROR: runtime log paths must be regular files' >&2
            return 1
        fi
    done
    if [ ! -e "${DEI_DIR}/dei835.log" ]; then
        (
            umask 077
            : >"${DEI_DIR}/dei835.log"
        ) || {
            echo 'ERROR: cannot create the private runtime log' >&2
            return 1
        }
    fi
    for dei_log_path in "${DEI_DIR}/dei835.log" "${DEI_DIR}/dei835.log.old"; do
        [ -f "${dei_log_path}" ] || continue
        # Reapply private permissions if the current mode cannot be read.
        dei_log_mode=$(stat -c %a "${dei_log_path}" 2>/dev/null) || dei_log_mode=
        if [ "${dei_log_mode}" != 600 ]; then
            chmod 600 "${dei_log_path}" || {
                echo 'ERROR: cannot make the runtime log private' >&2
                return 1
            }
        fi
    done
}

dei_flush_log() {
    [ -n "${DEI_LOG_BUFFER:-}" ] || return 0
    dei_log_ready || return 1
    if [ "${DEI_LOG_BUFFER}" = "${DEI_LAST_LOG_BUFFER:-}" ]; then
        DEI_LOG_BUFFER=
        return 0
    fi
    dei_log_size=$(wc -c <"${DEI_DIR}/dei835.log") || {
        echo 'ERROR: cannot determine runtime log size' >&2
        return 1
    }
    if [ "${dei_log_size}" -gt 32768 ]; then
        mv -f "${DEI_DIR}/dei835.log" "${DEI_DIR}/dei835.log.old" || {
            echo 'ERROR: cannot rotate the runtime log' >&2
            return 1
        }
        dei_log_ready || return 1
    fi
    dei_log_time=$(date '+%Y-%m-%d %H:%M:%S') || dei_log_time='timestamp unavailable'
    if ! printf '%s\n%s' "${dei_log_time}" "${DEI_LOG_BUFFER}" >>"${DEI_DIR}/dei835.log"; then
        echo 'ERROR: cannot persist the runtime log; pending records were retained' >&2
        return 1
    fi
    DEI_LAST_LOG_BUFFER=${DEI_LOG_BUFFER}
    DEI_LOG_BUFFER=
}

# Emit one record per actual filter, excluding tc's duplicate summary header.
# Fields: protocol|priority|handle|VID|skip_sw|extra match|action count|action.
dei_records() {
    awk '
    function flush() {
        if(active) print proto "|" pref "|" handle "|" vid "|" sw "|" extra "|" actions "|" action
    }
    /^filter / {
        flush(); active=0; proto=""; pref=""; handle=""; vid=""; sw=0; extra=0; actions=0; action=""
        for(i=1;i<=NF;i++) {
            if($i=="protocol") proto=$(i+1)
            if($i=="pref") pref=$(i+1)
            if($i=="handle") {handle=$(i+1); active=1}
            if($i=="chain" && $(i+1)!="0") extra=1
        }
        next
    }
    !active || NF==0 {next}
    $1=="vlan_id" {vid=$2; if(NF!=2) extra=1; next}
    $1=="skip_sw" {sw=1; next}
    $1=="in_hw" || $1=="in_hw_count" {next}
    $1=="action" && $2=="order" {
        actions++; action=""
        for(i=4;i<=NF;i++) action=action (i==4?"":" ") $i
        next
    }
    $1=="index" || $1=="ref" || $1=="bind" || $1=="random" {next}
    {extra=1}
    END {flush()}'
}

# Generic explicitly tagged service. Only the exact observed 835->836
# transformation authorizes a tag rewrite; other tagged services stay native.
dei_rule() {
    dei_records | awk -F'|' -v vlan="${DEI_VLAN:-835}" '
    $1=="802.1Q" && $4==vlan {
        count++
        if($7!=1) result="unsafe"
        else if($8=="gact action pass") result="native"
        else if($8=="vlan modify id " vlan " protocol 802.1Q pass") result="fixed"
        else if(vlan==835 && $8=="vlan modify id 836 protocol 802.1Q pass" && $2==1 && $3=="0x1" && $5==1 && $6==0) result="repair"
        else result="unsafe"
    }
    END {if(count==0) print "none"; else if(count!=1) print "unsafe"; else print result}'
}

# Recognize the exact untagged VLAN-836 profile observed on this module,
# including every intermediate state of our three targeted replacements.
# Input is records prefixed by I| (ingress) or E| (egress).
dei_836_profile() {
    awk -F'|' '
    {
        side=$1; proto=$2; pref=$3; handle=$4; vid=$5; sw=$6; extra=$7; actions=$8; action=$9
        if(side=="I") {
            icount++
            if(proto=="802.1Q" && pref==1 && handle=="0x1" && vid=="" && sw==1 && extra==0 && actions==1) {
                if(action=="vlan pop pass") istate="pop"
                if(action=="vlan modify id 836 protocol 802.1Q pass") istate="keep"
            }
            if(action=="vlan pop pass" && vid=="") haspop=1
        } else if(side=="E") {
            ecount++
            if(action=="vlan push id 836 protocol 802.1Q priority 0 pass") haspush=1
            if(vid==836) hasvid=1
            if(proto=="802.1Q" && pref==1 && handle=="0x1" && sw==1 && extra==0 && actions==1) {
                if(vid=="" && action=="gact action drop") pstate="drop"
                if(vid==836 && action=="vlan modify id 836 protocol 802.1Q pass") pstate="allow"
            }
            if(proto=="all" && pref==2 && handle=="0x2" && vid=="" && sw==1 && extra==0 && actions==1) {
                if(action=="vlan push id 836 protocol 802.1Q priority 0 pass") fstate="push"
                if(action=="gact action drop") fstate="drop"
            }
        }
    }
    END {
        if(icount==1 && ecount==2 && istate!="" && pstate!="" && fstate!="") {
            print "legacy836-" istate "-" pstate "-" fstate
        } else if(haspush || (haspop && hasvid)) print "unsafe"
        else print "none"
    }'
}

dei_mapper_rule() {
    dei_mapper=$1
    dei_egress=$(tc filter show dev "${dei_mapper}" egress 9>&- 2>/dev/null) || return 1
    dei_positive=$(printf '%s\n' "${dei_egress}" | dei_rule)
    if [ "${DEI_VLAN}" = 836 ]; then
        dei_ingress=$(tc filter show dev "${dei_mapper}" ingress 9>&- 2>/dev/null) || return 1
        dei_profile=$({
            printf '%s\n' "${dei_ingress}" | dei_records | sed 's/^/I|/'
            printf '%s\n' "${dei_egress}" | dei_records | sed 's/^/E|/'
        } | dei_836_profile)
        case "${dei_profile}" in
            none) ;; # Check the tagged rules below.
            legacy836-* | unsafe)
                printf '%s\n' "${dei_profile}"
                return 0
                ;;
            *) return 1 ;;
        esac
    fi
    printf '%s\n' "${dei_positive}"
}

# Capture only numeric driver fields; never include raw interface/MAC output in logs.
# Record: actual port ID | traffic type | direction | multicast | encryption.
dei_gem_record() {
    ip -d link show dev "gem$1" 9>&- 2>/dev/null | awk '
    function number(value) {return value ~ /^[0-9]+$/ ? value : "unknown"}
    $1=="gem" {
        count++
        id=""; type=""; dir=""; mc=""; enc=""
        for(i=1;i<=NF;i++) {
            if($i=="id:") id=$(i+1)
            if($i=="type:") type=$(i+1)
            if($i=="dir:") dir=$(i+1)
            if($i=="mc:") mc=$(i+1)
            if($i=="enc:") enc=$(i+1)
        }
    }
    END {
        if(count==1) print number(id) "|" number(type) "|" number(dir) "|" number(mc) "|" number(enc)
    }'
}

dei_record_encryption() {
    awk -F'|' -v wanted="$1" '
        NF==5 && $1==wanted && $2=="0" && $3=="3" && $4=="0" && $5 ~ /^[013]$/ {print $5}'
}

dei_gem_detail() {
    dei_gem_record "$1" | dei_record_encryption "$1"
}

# G.988 class 268 defines separate ME-instance and GEM Port-ID attributes.
# Accept only one successful two-byte hexadecimal attribute response; this
# decoder does not infer a mapping from an encryption value or interface name.
dei_omci_port_id() {
    awk '
    NF {
        count++
        if($1!="errorcode=0") invalid=1
        data=$2
        if(substr(data,1,10)!="attr_data=") invalid=1
        sub(/^attr_data=/,"",data)
        if(NF==3 && length(data)==2 && length($3)==2) data=data $3
        else if(NF!=2) invalid=1
        if(length(data)!=4 || data !~ /^[0-9A-Fa-f]+$/) invalid=1
    }
    END {
        if(count!=1 || invalid) exit 1
        digits="0123456789abcdef"; value=0; data=tolower(data)
        for(i=1;i<=4;i++) value=value*16+index(digits,substr(data,i,1))-1
        printf "%d\n",value
    }'
}

dei_omci_error_code() {
    awk '
    NF {
        records++
        for(i=1;i<=NF;i++) if($i ~ /^errorcode=/) {
            count++; code=$i; sub(/^errorcode=/,"",code)
            if(code !~ /^-?[0-9]+$/) invalid=1
        }
    }
    END {
        if(records==1 && count==1 && !invalid) print code
        else print "unavailable"
    }'
}

dei_verify_me_port() {
    DEI_OMCI_MAPPING_ERROR=
    # Never timeout or kill this firmware wrapper: it owns an internal lock.
    dei_me_reply=$(omci_pipe.sh meadg 268 "$1" 1 9>&- 2>/dev/null) || {
        DEI_OMCI_MAPPING_ERROR="GEM $1: cannot read OMCI ME 268 instance $1 Port-ID; no changes authorized"
        return 1
    }
    dei_me_port=$(printf '%s\n' "${dei_me_reply}" | dei_omci_port_id) || {
        DEI_OMCI_MAPPING_ERROR="GEM $1: invalid OMCI ME 268 instance $1 Port-ID response; no changes authorized"
        return 1
    }
    if [ "${dei_me_port}" != "$1" ]; then
        DEI_OMCI_MAPPING_ERROR="GEM $1: OMCI ME 268 instance $1 refers to Port-ID ${dei_me_port}; unsupported mapping, no changes authorized"
        return 1
    fi
}

dei_preflight_me_ports() {
    # Validate every changed target before any TC or OMCI mutation. The supported
    # profile uses equal IDs, but equality must be observed rather than assumed.
    for dei_verify_gem in ${DEI_GEMS}; do
        dei_verify_enc=$(dei_gem_detail "${dei_verify_gem}")
        [ "${dei_verify_enc}" != "${DEI_ENCRYPTION}" ] || continue
        case "${dei_verify_enc}" in
            0 | 1 | 3) ;;
            *)
                dei_log "GEM ${dei_verify_gem} changed during inspection; retry later"
                return 1
                ;;
        esac
        dei_verify_me_port "${dei_verify_gem}" || {
            dei_log "${DEI_OMCI_MAPPING_ERROR}"
            return 1
        }
    done
}

# Interface names are observations only: this inventory never chooses apply targets.
dei_report_inventory() {
    dei_inventory=$(
        for dei_inventory_path in "${DEI_NET}"/gem*; do
            [ -e "${dei_inventory_path}" ] || continue
            dei_inventory_id=${dei_inventory_path##*/gem}
            case "${dei_inventory_id}" in '' | *[!0-9]*) ;; *) printf '%s\n' "${dei_inventory_id}" ;; esac
        done | sort -nu | tr '\n' ' '
    )
    dei_inventory=${dei_inventory% }
    dei_log "Observed numeric GEM interfaces (all services; not an apply target): ${dei_inventory:-none}"
}

# Reuse the inspected snapshot rather than rereading devices while reporting.
# dei_log prints to stdout and buffers a log only when the caller opts in.
dei_report_discovery() {
    dei_report_inventory
    if [ "${DEI_MAPPER_CONFIRMED:-0}" != 1 ]; then
        dei_log "GEM discovery unavailable: ${DEI_ERROR:-service mapper is not verified}"
        return 0
    fi
    dei_log "VLAN ${DEI_VLAN}: mapper=${DEI_MAPPER} rules=${DEI_RULE} GEMs=${DEI_GEMS:-none} target-encryption=${DEI_ENCRYPTION} normalize=${DEI_NORMALIZE}"
    dei_log "Discovery source: current mapper membership; no fixed GEM range or assumption that IDs are contiguous."
    while IFS='|' read -r dei_member dei_id dei_type dei_direction dei_multicast dei_current; do
        [ -n "${dei_member}" ] || continue
        dei_role=unsupported
        if [ "${dei_type}" = 0 ] && [ "${dei_direction}" = 3 ] && [ "${dei_multicast}" = 0 ]; then
            dei_role=unicast-data
        elif [ "${dei_multicast}" = 1 ]; then
            dei_role=multicast
        fi
        dei_log "GEM ${dei_member}: role=${dei_role} driver-id=${dei_id} traffic-type=${dei_type} direction=${dei_direction} multicast=${dei_multicast} encryption=${dei_current} target-encryption=${DEI_ENCRYPTION}"
    done <<EOF
${DEI_GEM_DETAILS:-}
EOF
    if [ "${DEI_DISCOVERY_VALID:-0}" != 1 ]; then
        dei_log "Discovery blocked: ${DEI_ERROR:-GEM validation incomplete}; no changes authorized."
    fi
}

dei_discover() {
    DEI_MAPPER=
    DEI_GEMS=
    DEI_RULE=
    DEI_ERROR=
    DEI_MAPPER_CONFIRMED=0
    DEI_DISCOVERY_VALID=0
    DEI_GEM_DETAILS=
    DEI_SERVICE_IDENTITY=
    dei_runtime_config || return 1
    dei_ready || {
        DEI_ERROR=${DEI_READY_ERROR}
        return 1
    }
    for dei_path in "${DEI_NET}"/pmapper*; do
        [ -e "${dei_path}" ] || continue
        dei_dev=${dei_path##*/}
        dei_num=${dei_dev#pmapper}
        case "${dei_num}" in '' | *[!0-9]*) continue ;; *) ;; esac
        dei_kind=$(dei_mapper_rule "${dei_dev}") || {
            DEI_ERROR="cannot read ${dei_dev} service rules"
            return 1
        }
        case "${dei_kind}" in
            none) continue ;;
            unsafe)
                DEI_ERROR="unsupported VLAN-${DEI_VLAN} rule on ${dei_dev}; no changes"
                return 1
                ;;
            native | fixed | repair | legacy836-*) ;;
            *)
                DEI_ERROR="cannot classify ${dei_dev}"
                return 1
                ;;
        esac
        [ -z "${DEI_MAPPER}" ] || {
            DEI_ERROR="multiple VLAN-${DEI_VLAN} mappers; no changes"
            return 1
        }
        DEI_MAPPER=${dei_dev}
        DEI_RULE=${dei_kind}
    done
    [ -n "${DEI_MAPPER}" ] || {
        DEI_ERROR="waiting for VLAN-${DEI_VLAN} service mapper"
        return 1
    }
    DEI_MAPPER_CONFIRMED=1
    dei_link=$(ip -d link show dev "${DEI_MAPPER}" 9>&- 2>/dev/null) || {
        DEI_ERROR="cannot read ${DEI_MAPPER}"
        return 1
    }
    DEI_GEMS=$(printf '%s\n' "${dei_link}" | awk '
        $1=="pmapper" {for(i=1;i<=NF;i++) if($i ~ /^gem[0-9]+$/) {g=$i; sub(/^gem/,"",g); print g}}
        ' | sort -nu | tr '\n' ' ')
    DEI_GEMS=${DEI_GEMS% }
    [ -n "${DEI_GEMS}" ] || {
        DEI_ERROR="waiting for GEM members on ${DEI_MAPPER}"
        return 1
    }
    dei_invalid_gem=
    for dei_gem in ${DEI_GEMS}; do
        dei_record=$(dei_gem_record "${dei_gem}")
        [ -n "${dei_record}" ] || dei_record="unknown|unknown|unknown|unknown|unknown"
        DEI_GEM_DETAILS="${DEI_GEM_DETAILS}${dei_gem}|${dei_record}
"
        dei_enc=$(printf '%s\n' "${dei_record}" | dei_record_encryption "${dei_gem}")
        case "${dei_enc}" in
            0 | 1 | 3) ;;
            *) [ -n "${dei_invalid_gem}" ] || dei_invalid_gem=${dei_gem} ;;
        esac
    done
    if [ -n "${dei_invalid_gem}" ]; then
        DEI_ERROR="GEM ${dei_invalid_gem} is not a supported unicast data GEM; no changes"
        return 1
    fi
    DEI_DISCOVERY_VALID=1
    dei_ifindex=$(printf '%s\n' "${dei_link}" | awk 'NR==1 {print $1}')
    DEI_SERVICE_IDENTITY="${DEI_CONFIG_SIGNATURE}:${dei_ifindex}:${DEI_MAPPER}:${DEI_GEMS}"
    # The entry point compares successive discovery snapshots before applying changes.
    # shellcheck disable=SC2034
    DEI_SIGNATURE="${DEI_CONFIG_SIGNATURE}:${dei_ifindex}:${DEI_MAPPER}:${DEI_RULE}:${DEI_GEMS}"
}

dei_revalidate_service() {
    if ! dei_discover; then
        DEI_APPLY_INVALIDATED=1
        dei_log "Service revalidation failed: ${DEI_ERROR}; retry after fresh stable discovery"
        return 1
    fi
    if [ "${DEI_SERVICE_IDENTITY}" != "$1" ]; then
        DEI_APPLY_INVALIDATED=1
        DEI_DISCOVERY_VALID=0
        DEI_ERROR="selected service changed during inspection"
        dei_log "${DEI_ERROR}; retry after fresh stable discovery"
        return 1
    fi
}

dei_revalidate_rule() {
    dei_revalidate_service "$1" || return 1
    if [ "${DEI_RULE}" != "$2" ]; then
        DEI_APPLY_INVALIDATED=1
        DEI_DISCOVERY_VALID=0
        dei_log "VLAN rule changed during inspection; retry after fresh stable discovery"
        return 1
    fi
}

dei_normalize_836() {
    # Read back the complete known profile after every step. A failed step can
    # be retried later because discovery accepts all known partial profiles.
    dei_profile=$(dei_mapper_rule "${DEI_MAPPER}") || return 1
    case "${dei_profile}" in legacy836-*) ;; *)
        dei_log "VLAN-836 profile changed; retry later"
        return 1
        ;;
    esac
    case "${dei_profile}" in
        legacy836-*-drop-*)
            dei_revalidate_rule "$1" "${dei_profile}" || return 1
            dei_log "Accepting tagged VLAN 836 on ${DEI_MAPPER}"
            tc filter replace dev "${DEI_MAPPER}" egress protocol 802.1Q pref 1 handle 0x1 \
                flower skip_sw vlan_id 836 action vlan modify id 836 protocol 802.1Q pass 9>&- || return 1
            ;;
        legacy836-*-allow-*) ;;
        *) return 1 ;;
    esac
    dei_profile=$(dei_mapper_rule "${DEI_MAPPER}") || return 1
    case "${dei_profile}" in legacy836-*-allow-*) ;; *)
        dei_log "VLAN-836 tagged rule readback failed"
        return 1
        ;;
    esac
    dei_revalidate_service "$1" || return 1
    case "${dei_profile}" in
        legacy836-pop-*)
            dei_revalidate_rule "$1" "${dei_profile}" || return 1
            dei_log "Preserving downstream VLAN 836 on ${DEI_MAPPER}"
            tc filter replace dev "${DEI_MAPPER}" ingress protocol 802.1Q pref 1 handle 0x1 \
                flower skip_sw action vlan modify id 836 protocol 802.1Q pass 9>&- || return 1
            ;;
        legacy836-keep-*) ;;
        *) return 1 ;;
    esac
    dei_profile=$(dei_mapper_rule "${DEI_MAPPER}") || return 1
    case "${dei_profile}" in legacy836-keep-allow-*) ;; *)
        dei_log "VLAN-836 downstream rule readback failed"
        return 1
        ;;
    esac
    dei_revalidate_service "$1" || return 1
    case "${dei_profile}" in
        *-push)
            dei_revalidate_rule "$1" "${dei_profile}" || return 1
            dei_log "Removing legacy untagged VLAN-836 forwarding on ${DEI_MAPPER}"
            tc filter replace dev "${DEI_MAPPER}" egress protocol all pref 2 handle 0x2 \
                flower skip_sw action drop 9>&- || return 1
            ;;
        *-drop) ;;
        *) return 1 ;;
    esac
    dei_profile=$(dei_mapper_rule "${DEI_MAPPER}") || return 1
    [ "${dei_profile}" = legacy836-keep-allow-drop ] || {
        dei_log "VLAN-836 normalization readback failed"
        return 1
    }
}

dei_apply() {
    DEI_APPLY_INVALIDATED=0
    dei_expected_service=${DEI_SERVICE_IDENTITY}
    # A log failure must not silently permit mutations or claim full success.
    if [ "${DEI_LOGGING:-0}" = 1 ]; then dei_log_ready || return 1; fi
    dei_expected_config=${DEI_CONFIG_SIGNATURE}
    dei_runtime_config || {
        dei_log "${DEI_ERROR}"
        return 1
    }
    [ "${dei_expected_config}" = "${DEI_CONFIG_SIGNATURE}" ] || {
        # The watcher discards its stability sample after an invalidated apply.
        # shellcheck disable=SC2034
        DEI_APPLY_INVALIDATED=1
        dei_log "Runtime configuration changed during inspection; retry later"
        return 1
    }
    if ! dei_fix_mode=$(fw_printenv -n 8311_fix_vlans 2>/dev/null) || [ "${dei_fix_mode}" != 2 ]; then
        dei_log "Fix VLANs is not hook-only mode 2; no changes"
        return 1
    fi
    dei_revalidate_service "${dei_expected_service}" || return 1
    dei_preflight_me_ports || return 1
    # Attribute reads can block while the OLT reprovisions. Do not carry the
    # service selection made before those reads into a mutation.
    dei_revalidate_service "${dei_expected_service}" || return 1
    if [ "${DEI_NORMALIZE}" = true ]; then
        case "${DEI_RULE}" in
            repair)
                dei_verify=$(dei_mapper_rule "${DEI_MAPPER}") || return 1
                [ "${dei_verify}" = repair ] || {
                    dei_log "VLAN rule changed during inspection; retry later"
                    return 1
                }
                dei_revalidate_rule "${dei_expected_service}" repair || return 1
                dei_log "Repairing ${DEI_MAPPER} upstream VLAN 835 -> 835"
                tc filter replace dev "${DEI_MAPPER}" egress protocol 802.1Q pref 1 handle 0x1 \
                    flower skip_sw vlan_id 835 action vlan modify id 835 protocol 802.1Q pass 9>&- || return 1
                dei_verify=$(dei_mapper_rule "${DEI_MAPPER}") || return 1
                [ "${dei_verify}" = fixed ] || {
                    dei_log "VLAN repair readback failed: ${dei_verify}"
                    return 1
                }
                ;;
            legacy836-*) dei_normalize_836 "${dei_expected_service}" || return 1 ;;
            native | fixed) ;;
            *) return 1 ;;
        esac
    fi
    for dei_apply_gem in ${DEI_GEMS}; do
        dei_revalidate_service "${dei_expected_service}" || return 1
        dei_apply_enc=$(dei_gem_detail "${dei_apply_gem}")
        [ "${dei_apply_enc}" != "${DEI_ENCRYPTION}" ] || continue
        case "${dei_apply_enc}" in
            0 | 1 | 3) ;;
            *)
                dei_log "GEM ${dei_apply_gem} changed during inspection; retry later"
                return 1
                ;;
        esac
        # Revalidate the ME alias immediately before the write in case the OLT
        # recreated an instance after the complete preflight above.
        dei_verify_me_port "${dei_apply_gem}" || {
            dei_log "${DEI_OMCI_MAPPING_ERROR}"
            return 1
        }
        # This closes observed reprovisioning windows, not an atomic transaction
        # with the OLT: recheck the complete service after the blocking ME read.
        dei_revalidate_service "${dei_expected_service}" || return 1
        dei_log "GEM ${dei_apply_gem} encryption ${dei_apply_enc} -> ${DEI_ENCRYPTION}"
        # Do not timeout/kill omci_pipe.sh: it owns an internal firmware lock.
        dei_reply=$(omci_pipe.sh meads 268 "${dei_apply_gem}" 10 "${DEI_ENCRYPTION}" 9>&- 2>&1)
        dei_reply_status=$?
        dei_reply_code=$(printf '%s\n' "${dei_reply}" | dei_omci_error_code)
        if [ "${dei_reply_status}" != 0 ] || [ "${dei_reply_code}" != 0 ]; then
            dei_log "GEM ${dei_apply_gem} apply failed: errorcode=${dei_reply_code} command-exit=${dei_reply_status}"
            return 1
        fi
        dei_reply=$(omci_pipe.sh meadg 268 "${dei_apply_gem}" 10 9>&- 2>&1)
        dei_reply_status=$?
        dei_reply_code=$(printf '%s\n' "${dei_reply}" | dei_omci_error_code)
        if [ "${dei_reply_status}" != 0 ] || [ "${dei_reply_code}" != 0 ] ||
            ! printf '%s\n' "${dei_reply}" | grep -Eq "^[[:space:]]*errorcode=0[[:space:]]+attr_data=0${DEI_ENCRYPTION}[[:space:]]*$"; then
            dei_log "GEM ${dei_apply_gem} readback failed: errorcode=${dei_reply_code} command-exit=${dei_reply_status}"
            return 1
        fi
        if ! dei_applied_enc=$(dei_gem_detail "${dei_apply_gem}") || [ "${dei_applied_enc}" != "${DEI_ENCRYPTION}" ]; then
            dei_log "GEM ${dei_apply_gem} driver has not applied encryption ${DEI_ENCRYPTION}; retry later"
            return 1
        fi
    done
}
