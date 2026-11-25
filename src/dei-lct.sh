#!/bin/sh
# Optional management-port ("LCT") recovery. Loaded only when the TOML sets
# [management_recovery] enabled = true. OMCI provisioning can empty the LCT
# bridge port's forwarding map and move the port into a service bridge, which
# silences SSH and the web interface. Reads are bounded and read-only. The
# single write restores one verified, stable preimage and is read back.

# Supported layout, as recorded by the DEI guide on 8311 v2.8.3 (WAS-110): the
# LCT is sub-interface 1 of logical port 4 and forwards only to the CPU (map
# 0x1) in bridge 0. The bridge-port number is discovered, never assumed.
# Record fields: bridge | logical port | sub-interface | forwarding map[0].
DEI_LCT_HEALTHY='0x0|0x4|0x1|0x1'
DEI_LCT_TABLE_LAST=127
DEI_LCT_FALLBACK_SECONDS=120
DEI_LCT_RESCAN_SECONDS=300
DEI_LCT_MAX_ATTEMPTS=3
# The shared library supplies the interface directory, logging, configuration
# and the module-command resolver. Refuse to load without it.
: "${DEI_NET:?load dei-835-lib.sh before dei-lct.sh}"

dei_lct_prepare() {
    DEI_LCT_ERROR=
    dei_lct_timeout=${DEI835_SWITCH_TIMEOUT_SECONDS:-10}
    case "${dei_lct_timeout}" in
        [1-9] | [12][0-9] | 30) ;;
        *)
            DEI_LCT_ERROR='invalid switch timeout'
            return 1
            ;;
    esac
    if ! command -v timeout >/dev/null 2>&1 || ! dei_find_module_command switch_cli; then
        DEI_LCT_ERROR='cannot locate a bounded module switch_cli command'
        return 1
    fi
    DEI_LCT_SWITCH=${DEI_MODULE_COMMAND:?}
}

dei_lct_failsafe() {
    dei_lct_marker="${DEI835_MODULE_ROOT:-}/ptconf/.failsafe"
    [ -e "${dei_lct_marker}" ] || [ -L "${dei_lct_marker}" ]
}

# Print one line per requested bridge port: "<id> ok <record>", "<id> absent",
# "<id> invalid" or "<id> timeout". Only switch_cli is bounded, and it must not
# inherit an installer or worker lock. A killed read ends the dump early.
dei_lct_dump() {
    dei_lct_index=$1
    while [ "${dei_lct_index}" -le "$2" ]; do
        printf 'dei-lct-port %s\n' "${dei_lct_index}"
        timeout -s KILL "${dei_lct_timeout}" "${DEI_LCT_SWITCH}" GSW_BRIDGE_PORT_CONFIG_GET \
            "nBridgePortId=${dei_lct_index}" 7>&- 8>&- 9>&- 2>/dev/null
        dei_lct_status=$?
        printf '\ndei-lct-exit %s\n' "${dei_lct_status}"
        [ "${dei_lct_status}" != 137 ] || break
        dei_lct_index=$((dei_lct_index + 1))
    done | awk '
    function canon(value) {
        value=tolower(value)
        if(value !~ /^0x[0-9a-f]+$/ || length(value)>10) return ""
        sub(/^0x0*/,"",value)
        return "0x" (value=="" ? "0" : value)
    }
    function take(name, value) {
        value=canon(value)
        if(value=="" || ((name in seen) && seen[name]!=value)) invalid=1
        seen[name]=value
    }
    $1=="dei-lct-port" && NF==2 {id=$2; invalid=0; split("",seen); next}
    $1=="dei-lct-exit" && NF==2 {
        if(id=="") next
        if($2=="137") print id, "timeout"
        else if($2!="0") print id, "absent"
        else if(invalid || !("bridge" in seen) || !("port" in seen) || !("subif" in seen) || !("map" in seen)) print id, "invalid"
        else print id, "ok", seen["bridge"] "|" seen["port"] "|" seen["subif"] "|" seen["map"]
        id=""
        next
    }
    id=="" {next}
    $1=="nBridgeId" {take("bridge",$NF)}
    $1=="nDestLogicalPortId" {take("port",$NF)}
    $1=="nDestSubIfIdGroup" {take("subif",$NF)}
    $1=="nBridgePortMapIndex[0]" {take("map",$NF)}'
}

dei_lct_read() {
    DEI_LCT_RECORD=
    dei_lct_line=$(dei_lct_dump "$1" "$1") || return 1
    case "${dei_lct_line}" in
        "$1 ok "*) DEI_LCT_RECORD=${dei_lct_line#"$1 ok "} ;;
        *) return 1 ;;
    esac
}

# The interface name must corroborate the supported layout before any switch
# read: exactly one <UNI>_1_lct interface whose parent UNI also exists.
dei_lct_interface() {
    DEI_LCT_DEV=
    dei_lct_count=0
    for dei_lct_path in "${DEI_NET}"/*_lct; do
        [ -e "${dei_lct_path}" ] || continue
        dei_lct_count=$((dei_lct_count + 1))
        DEI_LCT_DEV=${dei_lct_path##*/}
    done
    if [ "${dei_lct_count}" != 1 ]; then
        DEI_LCT_ERROR="expected exactly one LCT interface, found ${dei_lct_count}"
        return 1
    fi
    dei_lct_parent=${DEI_LCT_DEV%_1_lct}
    case "${DEI_LCT_DEV}" in
        *[!A-Za-z0-9_]* | _1_lct) dei_lct_parent= ;;
        *_1_lct) ;;
        *) dei_lct_parent= ;;
    esac
    if [ -z "${dei_lct_parent}" ] || [ ! -e "${DEI_NET}/${dei_lct_parent}" ]; then
        DEI_LCT_ERROR='the LCT interface does not match the supported sub-interface layout'
        return 1
    fi
}

# Scan the whole bridge-port table and accept only a unique port that presents
# the LCT identity. Ambiguous, missing or unanswered identities stop changes.
dei_lct_locate() {
    DEI_LCT_PORT=
    DEI_LCT_RECORD=
    dei_lct_interface || return 1
    dei_lct_table=$(dei_lct_dump 1 "${DEI_LCT_TABLE_LAST}") || {
        DEI_LCT_ERROR='cannot read the switch bridge-port table'
        return 1
    }
    dei_lct_found=$(printf '%s\n' "${dei_lct_table}" | awk '
        $2=="timeout" {stalled=1}
        $2=="ok" && NF==3 {
            split($3,field,"|")
            if(field[2]=="0x4" && field[3]=="0x1") {count++; found=$1 " " $3}
        }
        END {
            if(stalled) print "timeout"
            else if(count==1) print found
            else if(count>1) print "ambiguous"
            else print "none"
        }')
    case "${dei_lct_found}" in
        timeout) DEI_LCT_ERROR='the switch did not answer within the bounded interval' ;;
        ambiguous) DEI_LCT_ERROR='several bridge ports present the LCT identity' ;;
        none | '') DEI_LCT_ERROR='no bridge port presents the supported LCT identity' ;;
        *)
            DEI_LCT_PORT=${dei_lct_found%% *}
            DEI_LCT_RECORD=${dei_lct_found#* }
            return 0
            ;;
    esac
    return 1
}

# Only the two detached preimages recorded by the guide authorize a restore.
dei_lct_classify() {
    case "$1" in
        "${DEI_LCT_HEALTHY}") DEI_LCT_STATE=healthy ;;
        '0x0|0x4|0x1|0x0' | '0x1|0x4|0x1|0x0') DEI_LCT_STATE=detached ;;
        *'|0x4|0x1|'*) DEI_LCT_STATE=unsupported ;;
        *) DEI_LCT_STATE=changed ;;
    esac
}

dei_lct_describe() {
    dei_lct_fields=$1
    dei_lct_bridge=${dei_lct_fields%%|*}
    dei_lct_fields=${dei_lct_fields#*|}
    dei_lct_logical=${dei_lct_fields%%|*}
    dei_lct_fields=${dei_lct_fields#*|}
    printf 'bridge=%s logical-port=%s sub-interface=%s map=%s' \
        "${dei_lct_bridge}" "${dei_lct_logical}" "${dei_lct_fields%%|*}" "${dei_lct_fields#*|}"
}

# Reprovisioning recreates service interfaces. A changed inventory restarts the
# stability window, so the fallback cannot fire while provisioning is moving.
dei_lct_inventory() {
    for dei_lct_path in "${DEI_NET}"/gem* "${DEI_NET}"/pmapper*; do
        [ -e "${dei_lct_path}" ] || continue
        printf '%s ' "${dei_lct_path##*/}"
    done
}

dei_lct_note() {
    [ "$1" != "${DEI_LCT_LAST_NOTE:-}" ] || return 0
    DEI_LCT_LAST_NOTE=$1
    dei_log "$1"
}

dei_lct_reset() {
    DEI_LCT_SEEN=
    DEI_LCT_STREAK=0
    DEI_LCT_ELAPSED=0
}

# Drop the remembered port and every pending sample, for example when the
# operator turns recovery off while the watcher keeps running.
dei_lct_forget() {
    DEI_LCT_PORT=
    DEI_LCT_WAIT=0
    DEI_LCT_ATTEMPTS=0
    DEI_LCT_FAILED=
    DEI_LCT_LAST_NOTE=
    dei_lct_reset
}

# Read-only summary for check and status. Never writes or keeps state.
dei_lct_report() {
    if dei_lct_failsafe; then
        dei_log 'Management port: firmware failsafe is present; the switch was not inspected.'
        return 0
    fi
    if ! dei_lct_prepare || ! dei_lct_locate; then
        dei_log "Management port: ${DEI_LCT_ERROR}; no changes authorized."
        return 0
    fi
    dei_lct_classify "${DEI_LCT_RECORD}"
    dei_lct_summary=$(dei_lct_describe "${DEI_LCT_RECORD}")
    dei_log "Management port: interface=${DEI_LCT_DEV} bridge-port=${DEI_LCT_PORT} state=${DEI_LCT_STATE} ${dei_lct_summary}"
    case "${DEI_LCT_STATE}" in
        healthy) ;;
        detached) dei_log 'Management port: a recognized detached state; the watcher restores it after stable samples.' ;;
        *) dei_log 'Management port: not a recognized state; no changes authorized.' ;;
    esac
    DEI_LCT_PORT=
}

dei_lct_restore() {
    dei_lct_expected=${DEI_LCT_RECORD}
    dei_lct_before=$(dei_lct_describe "${dei_lct_expected}")
    # Recheck everything that authorized this write immediately before it.
    if ! dei_runtime_config || [ "${DEI_LCT_ENABLED:-unknown}" != true ]; then
        dei_lct_reset
        dei_lct_note 'Management port: configuration changed during inspection; retry later'
        return 0
    fi
    if ! dei_lct_mode=$(fw_printenv -n 8311_fix_vlans 2>/dev/null) || [ "${dei_lct_mode}" != 2 ]; then
        dei_lct_reset
        dei_lct_note 'Management port: Fix VLANs is not hook-only mode 2; no changes'
        return 0
    fi
    if dei_lct_failsafe; then
        dei_lct_reset
        dei_lct_note 'Management port: firmware failsafe is present; no switch reads or writes'
        return 0
    fi
    if ! dei_lct_read "${DEI_LCT_PORT}" || [ "${DEI_LCT_RECORD}" != "${dei_lct_expected}" ]; then
        dei_lct_reset
        dei_lct_note 'Management port changed during inspection; retry after fresh stable samples'
        return 0
    fi
    DEI_LCT_LAST_NOTE=
    dei_log "Restoring management port ${DEI_LCT_DEV} bridge-port=${DEI_LCT_PORT}: ${dei_lct_before} ($1)"
    # The guide's hardware-validated restore, addressed to the discovered port.
    timeout -s KILL "${dei_lct_timeout}" "${DEI_LCT_SWITCH}" GSW_BRIDGE_PORT_CONFIG_SET bForce=1 \
        "nBridgePortId=${DEI_LCT_PORT}" nBridgeId=0 nDestLogicalPortId=4 nDestSubIfIdGroup=1 \
        bEgressExtendedVlanEnable=0 nEgressExtendedVlanBlockId=0 bBridgePortMapEnable=1 Index=0 MapValue=0x1 \
        7>&- 8>&- 9>&- >/dev/null 2>&1
    dei_lct_status=$?
    dei_lct_reset
    if dei_lct_read "${DEI_LCT_PORT}" && [ "${DEI_LCT_RECORD}" = "${DEI_LCT_HEALTHY}" ]; then
        DEI_LCT_ATTEMPTS=0
        DEI_LCT_FAILED=
        dei_lct_after=$(dei_lct_describe "${DEI_LCT_RECORD}")
        DEI_LCT_LAST_NOTE="Management port healthy: ${DEI_LCT_DEV} bridge-port=${DEI_LCT_PORT} ${dei_lct_after}"
        dei_log "Management port restored: ${DEI_LCT_DEV} bridge-port=${DEI_LCT_PORT} ${dei_lct_after}"
        return 0
    fi
    DEI_LCT_ATTEMPTS=$((${DEI_LCT_ATTEMPTS:-0} + 1))
    DEI_LCT_FAILED=${dei_lct_expected}
    dei_log "Management port restore failed: command-exit=${dei_lct_status}, readback is not the healthy state (attempt ${DEI_LCT_ATTEMPTS} of ${DEI_LCT_MAX_ATTEMPTS})"
    return 0
}

# One watcher polling cycle. $1 is 1 only when the selected service was
# verified healthy in this same cycle.
dei_lct_watch() {
    dei_lct_interval=${DEI_INTERVAL:?the watcher polling interval is required}
    if dei_lct_failsafe; then
        dei_lct_reset
        dei_lct_note 'Management port: firmware failsafe is present; no switch reads or writes'
        return 0
    fi
    if ! dei_lct_prepare; then
        dei_lct_reset
        dei_lct_note "Management port: ${DEI_LCT_ERROR}; no changes"
        return 0
    fi
    if [ -z "${DEI_LCT_PORT:-}" ]; then
        if [ "${DEI_LCT_WAIT:-0}" -gt 0 ]; then
            DEI_LCT_WAIT=$((DEI_LCT_WAIT - dei_lct_interval))
            return 0
        fi
        dei_lct_reset
        if ! dei_lct_locate; then
            # A full scan is costly on the module; do not repeat it every poll.
            DEI_LCT_WAIT=${DEI_LCT_RESCAN_SECONDS}
            dei_lct_note "Management port: ${DEI_LCT_ERROR}; no changes authorized"
            return 0
        fi
    elif ! dei_lct_read "${DEI_LCT_PORT}"; then
        dei_lct_reset
        dei_lct_note "Management port: bridge port ${DEI_LCT_PORT} is unreadable; no changes"
        return 0
    fi
    dei_lct_classify "${DEI_LCT_RECORD}"
    dei_lct_detail="${DEI_LCT_DEV} bridge-port=${DEI_LCT_PORT} $(dei_lct_describe "${DEI_LCT_RECORD}")"
    case "${DEI_LCT_STATE}" in
        healthy)
            dei_lct_reset
            DEI_LCT_ATTEMPTS=0
            DEI_LCT_FAILED=
            dei_lct_note "Management port healthy: ${dei_lct_detail}"
            return 0
            ;;
        detached) ;;
        unsupported)
            dei_lct_reset
            dei_lct_note "Management port in an unrecognized state: ${dei_lct_detail}; no changes authorized"
            return 0
            ;;
        *)
            dei_lct_note "Management port: bridge port ${DEI_LCT_PORT} no longer presents the LCT identity; rediscovering"
            DEI_LCT_PORT=
            dei_lct_reset
            return 0
            ;;
    esac
    [ "${DEI_LCT_RECORD}" = "${DEI_LCT_FAILED:-}" ] || DEI_LCT_ATTEMPTS=0
    dei_lct_sample="${DEI_LCT_RECORD}#$(dei_lct_inventory)"
    if [ "${dei_lct_sample}" = "${DEI_LCT_SEEN:-}" ]; then
        DEI_LCT_STREAK=$((DEI_LCT_STREAK + 1))
        DEI_LCT_ELAPSED=$((DEI_LCT_ELAPSED + dei_lct_interval))
    else
        DEI_LCT_SEEN=${dei_lct_sample}
        DEI_LCT_STREAK=1
        DEI_LCT_ELAPSED=0
    fi
    if [ "${DEI_LCT_ATTEMPTS:-0}" -ge "${DEI_LCT_MAX_ATTEMPTS}" ]; then
        dei_lct_note "Management port detached: ${dei_lct_detail}; restore suspended after ${DEI_LCT_MAX_ATTEMPTS} unsuccessful attempts"
        return 0
    fi
    if [ "${DEI_LCT_STREAK}" -lt 2 ]; then
        dei_lct_note "Management port detached: ${dei_lct_detail}; waiting for a matching sample"
        return 0
    fi
    if [ "$1" = 1 ]; then
        dei_lct_restore 'selected service is stable'
    elif [ "${DEI_LCT_ELAPSED}" -ge "${DEI_LCT_FALLBACK_SECONDS}" ]; then
        dei_lct_restore "fallback: unchanged for at least ${DEI_LCT_FALLBACK_SECONDS}s without a stable service"
    else
        dei_lct_note "Management port detached: ${dei_lct_detail}; waiting for a stable service or the ${DEI_LCT_FALLBACK_SECONDS}s fallback"
    fi
}

dei_lct_forget
