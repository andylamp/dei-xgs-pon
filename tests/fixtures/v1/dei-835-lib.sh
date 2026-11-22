#!/bin/sh
# DEI VLAN 835 repair, v1. No device identity, MIB or bridge-port changes.
DEI_DIR=${DEI835_CONF_DIR:-/ptconf/8311}
DEI_RUN=${DEI835_RUN_DIR:-/tmp/dei835}
DEI_NET=${DEI835_NET_DIR:-/sys/class/net}

dei_ready() {
    dei_pon=$(pon psg 9>&- 2>/dev/null) || return 1
    printf '%s\n' "$dei_pon" | awk '
        {for(i=1;i<=NF;i++) {if($i=="errorcode=0") ok=1; if($i=="current=51") ready=1}}
        END {exit !(ok && ready)}'
}

dei_log() {
    printf '%s\n' "$*"
    [ "${DEI_LOGGING:-0}" = 1 ] || return 0
    DEI_LOG_BUFFER="${DEI_LOG_BUFFER:-}$*
"
}

dei_flush_log() {
    [ -n "${DEI_LOG_BUFFER:-}" ] || return 0
    if [ "$DEI_LOG_BUFFER" = "${DEI_LAST_LOG_BUFFER:-}" ]; then
        DEI_LOG_BUFFER=
        return 0
    fi
    if [ -f "$DEI_DIR/dei835.log" ] && [ "$(wc -c < "$DEI_DIR/dei835.log")" -gt 32768 ]; then
        mv -f "$DEI_DIR/dei835.log" "$DEI_DIR/dei835.log.old"
    fi
    printf '%s\n%s' "$(date '+%Y-%m-%d %H:%M:%S')" "$DEI_LOG_BUFFER" >> "$DEI_DIR/dei835.log"
    DEI_LAST_LOG_BUFFER=$DEI_LOG_BUFFER
    DEI_LOG_BUFFER=
}

# Classify the one explicit 802.1Q VLAN-835 rule. Unknown matches/actions
# cannot authorize a rewrite. The native OMCI pass rule may use handle 0x2.
dei_rule() {
    awk '
    function flush() {
        if (!active || proto != "802.1Q" || vid != "835") return
        count++
        if (actions != 1) result="unsafe"
        else if (action == "gact action pass") result="native"
        else if (action == "vlan modify id 835 protocol 802.1Q pass") result="fixed"
        else if (action == "vlan modify id 836 protocol 802.1Q pass" && pref == "1" && handle == "0x1" && sw && !extra) result="repair"
        else result="unsafe"
    }
    /^filter / {
        flush(); active=0; proto=""; pref=""; handle=""; vid=""; sw=0; extra=0; actions=0; action=""
        for (i=1;i<=NF;i++) {
            if ($i=="protocol") proto=$(i+1)
            if ($i=="pref") pref=$(i+1)
            if ($i=="handle") { handle=$(i+1); active=1 }
            if ($i=="chain" && $(i+1)!="0") extra=1
        }
        next
    }
    !active {next}
    NF==0 {next}
    $1=="vlan_id" {vid=$2; if (NF!=2) extra=1; next}
    $1=="skip_sw" {sw=1; next}
    $1=="in_hw" || $1=="in_hw_count" {next}
    $1=="action" && $2=="order" {
        actions++; action=""
        for(i=4;i<=NF;i++) action=action (i==4?"":" ") $i
        next
    }
    $1=="index" || $1=="ref" || $1=="bind" {next}
    $1=="random" {next}
    {extra=1}
    END {flush(); if(count==0) print "none"; else if(count!=1) print "unsafe"; else print result}
    '
}

dei_gem_detail() {
    ip -d link show dev "gem$1" 9>&- 2>/dev/null | awk -v wanted="$1" '
    $1=="gem" {
        id=""; type=""; dir=""; mc=""; enc=""
        for(i=1;i<=NF;i++) {
            if($i=="id:") id=$(i+1)
            if($i=="type:") type=$(i+1)
            if($i=="dir:") dir=$(i+1)
            if($i=="mc:") mc=$(i+1)
            if($i=="enc:") enc=$(i+1)
        }
        if(id==wanted && type=="0" && dir=="3" && mc=="0" && enc ~ /^[013]$/) print enc
    }'
}

dei_discover() {
    DEI_MAPPER=; DEI_GEMS=; DEI_RULE=; DEI_ERROR=
    dei_ready || { DEI_ERROR="waiting for PON O5"; return 1; }
    for dei_path in "$DEI_NET"/pmapper*; do
        [ -e "$dei_path" ] || continue
        dei_dev=${dei_path##*/}
        dei_num=${dei_dev#pmapper}
        case "$dei_num" in ''|*[!0-9]*) continue;; esac
        dei_rules=$(tc filter show dev "$dei_dev" egress 9>&- 2>/dev/null) || {
            DEI_ERROR="cannot read $dei_dev egress rules"; return 1;
        }
        dei_kind=$(printf '%s\n' "$dei_rules" | dei_rule)
        case "$dei_kind" in
            none) continue;;
            unsafe) DEI_ERROR="unsupported VLAN-835 rule on $dei_dev; no changes"; return 1;;
            native|fixed|repair) ;;
            *) DEI_ERROR="cannot classify $dei_dev"; return 1;;
        esac
        [ -z "$DEI_MAPPER" ] || { DEI_ERROR="multiple VLAN-835 mappers; no changes"; return 1; }
        DEI_MAPPER=$dei_dev; DEI_RULE=$dei_kind
    done
    [ -n "$DEI_MAPPER" ] || { DEI_ERROR="waiting for VLAN-835 service mapper"; return 1; }
    dei_link=$(ip -d link show dev "$DEI_MAPPER" 9>&- 2>/dev/null) || {
        DEI_ERROR="cannot read $DEI_MAPPER"; return 1;
    }
    DEI_GEMS=$(printf '%s\n' "$dei_link" | awk '
        $1=="pmapper" {for(i=1;i<=NF;i++) if($i ~ /^gem[0-9]+$/) {g=$i; sub(/^gem/,"",g); print g}}
        ' | sort -nu | tr '\n' ' ')
    DEI_GEMS=${DEI_GEMS% }
    [ -n "$DEI_GEMS" ] || { DEI_ERROR="waiting for GEM members on $DEI_MAPPER"; return 1; }
    for dei_gem in $DEI_GEMS; do
        dei_enc=$(dei_gem_detail "$dei_gem")
        case "$dei_enc" in
            0|1|3) ;;
            *) DEI_ERROR="GEM $dei_gem is not a supported unicast data GEM; no changes"; return 1;;
        esac
    done
    dei_ifindex=$(printf '%s\n' "$dei_link" | awk 'NR==1 {print $1}')
    DEI_SIGNATURE="$dei_ifindex:$DEI_MAPPER:$DEI_RULE:$DEI_GEMS"
}

dei_apply() {
    [ "$(fw_printenv -n 8311_fix_vlans 2>/dev/null)" = 2 ] || {
        dei_log "Fix VLANs is not hook-only mode 2; no changes"; return 1;
    }
    dei_ready || { dei_log "PON left O5; retry after activation"; return 1; }
    if [ "$DEI_RULE" = repair ]; then
        dei_verify=$(tc filter show dev "$DEI_MAPPER" egress 9>&- | dei_rule)
        [ "$dei_verify" = repair ] || { dei_log "VLAN rule changed during inspection; retry later"; return 1; }
        dei_log "Repairing $DEI_MAPPER upstream VLAN 835 -> 835"
        tc filter replace dev "$DEI_MAPPER" egress protocol 802.1Q pref 1 handle 0x1 \
            flower skip_sw vlan_id 835 action vlan modify id 835 protocol 802.1Q pass 9>&- || return 1
        dei_verify=$(tc filter show dev "$DEI_MAPPER" egress 9>&- | dei_rule)
        [ "$dei_verify" = fixed ] || { dei_log "VLAN repair readback failed: $dei_verify"; return 1; }
    fi
    for dei_gem in $DEI_GEMS; do
        dei_ready || { dei_log "PON left O5; retry after activation"; return 1; }
        # Re-read immediately before writing; never change a multicast/unknown GEM.
        dei_enc=$(dei_gem_detail "$dei_gem")
        case "$dei_enc" in
            3) continue;;
            0|1) ;;
            *) dei_log "GEM $dei_gem changed during inspection; retry later"; return 1;;
        esac
        dei_log "GEM $dei_gem encryption $dei_enc -> 3 (downstream only)"
        # Do not timeout/kill omci_pipe.sh: it owns an internal firmware lock.
        dei_reply=$(omci_pipe.sh meads 268 "$dei_gem" 10 3 9>&- 2>&1)
        if ! printf '%s\n' "$dei_reply" | grep -Eq '(^|[[:space:]])errorcode=0([[:space:]]|$)'; then
            dei_log "GEM $dei_gem apply failed: $dei_reply"; return 1
        fi
        dei_reply=$(omci_pipe.sh meadg 268 "$dei_gem" 10 9>&- 2>&1)
        if ! printf '%s\n' "$dei_reply" | grep -Eq 'errorcode=0[[:space:]]+attr_data=03([[:space:]]|$)'; then
            dei_log "GEM $dei_gem readback failed: $dei_reply"; return 1
        fi
        [ "$(dei_gem_detail "$dei_gem")" = 3 ] || { dei_log "GEM $dei_gem driver has not applied encryption 3; retry later"; return 1; }
    done
}
