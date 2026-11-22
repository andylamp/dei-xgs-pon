#!/bin/sh
# Dependency-free parser for the documented DEI configuration TOML subset.
# Configuration is data: never source/eval it or interpolate it into commands.

dei_config_load() {
    CFG_CONFIG_ERROR=
    dei_cfg_path=$1
    if [ -z "$dei_cfg_path" ] || [ ! -f "$dei_cfg_path" ] || [ ! -r "$dei_cfg_path" ]; then
        CFG_CONFIG_ERROR="configuration file is missing or unreadable"
        printf 'Configuration error: %s\n' "$CFG_CONFIG_ERROR" >&2
        return 1
    fi
    dei_cfg_size=$(wc -c <"$dei_cfg_path")
    if [ "$dei_cfg_size" -gt 65536 ]; then
        CFG_CONFIG_ERROR="configuration exceeds 64 KiB"
        printf 'Configuration error: %s\n' "$CFG_CONFIG_ERROR" >&2
        return 1
    fi
    # LF, CR and horizontal tabs may format the file. Other control bytes,
    # including NUL, are rejected before awk can interpret/truncate them.
    dei_cfg_controls=$(LC_ALL=C tr -cd '\000-\010\013\014\016-\037\177' <"$dei_cfg_path" | wc -c)
    if [ "$dei_cfg_controls" -ne 0 ]; then
        CFG_CONFIG_ERROR="configuration contains unsupported control bytes"
        printf 'Configuration error: %s\n' "$CFG_CONFIG_ERROR" >&2
        return 1
    fi
    dei_cfg_parsed=$(LC_ALL=C awk '
    function fail(message) {
        bad=1
        print "Configuration error at line " NR ": " message > "/dev/stderr"
        exit 1
    }
    function trim(value) {
        sub(/^[ \t]+/, "", value)
        sub(/[ \t]+$/, "", value)
        return value
    }
    function field(name, type, default_value) {
        types[name]=type
        values[name]=default_value
        order[++field_count]=name
    }
    function quoted(raw, key,    quote, i, character, closing, remainder, value) {
        quote=substr(raw,1,1)
        closing=0
        for(i=2;i<=length(raw);i++) {
            character=substr(raw,i,1)
            if(character=="\\") fail("escapes/backslashes are unsupported for " key)
            if(character==quote) {closing=i; break}
        }
        if(!closing) fail("unterminated string for " key)
        remainder=trim(substr(raw,closing+1))
        if(remainder!="" && substr(remainder,1,1)!="#") fail("trailing data for " key)
        value=substr(raw,2,closing-2)
        if(value ~ /[[:cntrl:]]/) fail("control character in string for " key)
        if(length(value)>256) fail("string is too long for " key)
        return value
    }
    function ipv4(value,    octets, count, i) {
        count=split(value,octets,".")
        if(count!=4) return 0
        for(i=1;i<=4;i++) {
            if(octets[i] !~ /^(0|[1-9][0-9]*)$/ || length(octets[i])>3 || octets[i]+0>255) return 0
        }
        return 1
    }
    function unicast(value,    octets) {
        if(!ipv4(value)) return 0
        split(value,octets,".")
        return octets[1]+0>=1 && octets[1]+0<=223 && octets[1]+0!=127
    }
    function netmask(value,    octets, i, partial, octet) {
        if(!ipv4(value) || value=="0.0.0.0") return 0
        split(value,octets,".")
        partial=0
        for(i=1;i<=4;i++) {
            octet=octets[i]+0
            if(partial && octet!=0) return 0
            if(octet!=255) {
                if(octet!=254 && octet!=252 && octet!=248 && octet!=240 && octet!=224 && octet!=192 && octet!=128 && octet!=0) return 0
                partial=1
            }
        }
        return 1
    }
    BEGIN {
        single_quote=sprintf("%c",39)
        field("schema_version","integer","1")
        field("internet.vlan","integer","835")
        field("internet.encryption_key_ring","integer","3")
        field("internet.normalize_vlan","boolean","true")
        field("watcher.enabled","boolean","true")
        field("watcher.interval_seconds","integer","10")
        field("library_patch.enabled","boolean","false")
        field("library_patch.policy","string","auto")
        field("onu.serial","string","")
        field("onu.omcc_version","string","0xA0")
        field("onu.vendor_id","string","")
        field("onu.equipment_id","string","")
        field("onu.hardware_version","string","")
        field("onu.mib_profile","string","preserve")
        field("management.ip_address","string","")
        field("management.netmask","string","")
        field("management.gateway","string","")
        field("management.ping_ip","string","")
        field("management.failsafe_delay","integer","30")
        field("storage.persistent_root","string","auto")
        sections["internet"]=1
        sections["watcher"]=1
        sections["library_patch"]=1
        sections["onu"]=1
        sections["management"]=1
        sections["storage"]=1
    }
    {
        sub(/\r$/, "")
        if(index($0,"\r")) fail("unexpected carriage return")
        line=trim($0)
        if(line=="" || substr(line,1,1)=="#") next
        if(substr(line,1,1)=="[") {
            if(line !~ /^\[[a-z_]+\][ \t]*(#.*)?$/) fail("unsupported section syntax")
            section=line
            sub(/^\[/,"",section)
            sub(/\].*$/,"",section)
            if(!(section in sections)) fail("unknown section")
            if(section in seen_sections) fail("duplicate section")
            seen_sections[section]=1
            next
        }
        equals=index(line,"=")
        if(!equals) fail("expected key = value")
        key=trim(substr(line,1,equals-1))
        if(key !~ /^[a-z_][a-z_0-9]*$/) fail("unsupported key syntax")
        full=(section=="" ? key : section "." key)
        if(!(full in types)) fail("unknown field")
        if(full in seen) fail("duplicate field " full)
        seen[full]=1
        raw=trim(substr(line,equals+1))
        if(types[full]=="string") {
            first=substr(raw,1,1)
            if(first!="\"" && first!=single_quote) fail("expected quoted string for " full)
            values[full]=quoted(raw,full)
        } else {
            sub(/#.*/,"",raw)
            raw=trim(raw)
            if(types[full]=="boolean") {
                if(raw!="true" && raw!="false") fail("expected boolean for " full)
            } else {
                if(raw !~ /^(0|[1-9][0-9]*)$/ || length(raw)>10) fail("expected decimal integer for " full)
            }
            values[full]=raw
        }
    }
    END {
        if(bad) exit 1
        if(!seen["schema_version"]) fail("schema_version is required")
        if(values["schema_version"]!="1") fail("unsupported schema_version")
        if(values["internet.vlan"]!="835" && values["internet.vlan"]!="836") fail("internet.vlan must be 835 or 836")
        if(values["internet.encryption_key_ring"]!="1" && values["internet.encryption_key_ring"]!="3") fail("internet.encryption_key_ring must be 1 or 3")
        if(values["watcher.interval_seconds"]+0<5 || values["watcher.interval_seconds"]+0>300) fail("watcher.interval_seconds must be 5..300")
        if(values["management.failsafe_delay"]+0>300) fail("management.failsafe_delay must be 0..300")
        if(values["library_patch.policy"]!="auto" && values["library_patch.policy"]!="never") fail("library_patch.policy must be auto or never")
        if(values["onu.mib_profile"]!="preserve" && values["onu.mib_profile"]!="native_1u" && values["onu.mib_profile"]!="dei_0205") fail("unsupported onu.mib_profile")
        if(values["storage.persistent_root"]!="auto" && values["storage.persistent_root"]!="enabled" && values["storage.persistent_root"]!="preserve") fail("unsupported storage.persistent_root")
        serial=values["onu.serial"]
        if(serial!="" && (length(serial)!=12 || substr(serial,1,4) !~ /^[A-Za-z]+$/ || substr(serial,5) !~ /^[0-9A-Fa-f]+$/)) fail("onu.serial must be four letters followed by eight hexadecimal digits")
        omcc=values["onu.omcc_version"]
        if(length(omcc)!=4 || substr(omcc,1,2)!="0x" || substr(omcc,3) !~ /^[0-9A-Fa-f]+$/) fail("onu.omcc_version must have form 0xNN")
        omcc="0x" toupper(substr(omcc,3))
        if(omcc !~ /^0x(8[2-9A-F]|[9AB][0-9A-F])$/) fail("onu.omcc_version must be 0x82..0xBF for firmware v2.8.3")
        values["onu.omcc_version"]=omcc
        vendor=values["onu.vendor_id"]
        if(vendor!="" && (length(vendor)!=4 || vendor !~ /^[A-Za-z0-9]+$/)) fail("onu.vendor_id must be empty or four alphanumeric characters")
        if(length(values["onu.equipment_id"])>20) fail("onu.equipment_id exceeds 20 bytes")
        if(length(values["onu.hardware_version"])>14) fail("onu.hardware_version exceeds 14 bytes")
        for(key in values) {
            if(key=="management.ip_address" || key=="management.gateway" || key=="management.ping_ip") {
                if(values[key]!="" && !unicast(values[key])) fail(key " must be empty or a unicast IPv4 address")
            }
        }
        if(values["management.netmask"]!="" && !netmask(values["management.netmask"])) fail("management.netmask must be empty or a contiguous nonzero IPv4 mask")
        for(i=1;i<=field_count;i++) print order[i] "\t" values[order[i]]
    }' "$dei_cfg_path") || {
        CFG_CONFIG_ERROR="configuration validation failed"
        return 1
    }
    # A fixed dispatch assigns only schema fields. Values are never shell code.
    while IFS="$(printf '\t')" read -r dei_cfg_key dei_cfg_value; do
        case "$dei_cfg_key" in
            schema_version) CFG_SCHEMA_VERSION=$dei_cfg_value ;;
            internet.vlan) CFG_INTERNET_VLAN=$dei_cfg_value ;;
            internet.encryption_key_ring) CFG_INTERNET_ENCRYPTION_KEY_RING=$dei_cfg_value ;;
            internet.normalize_vlan) CFG_INTERNET_NORMALIZE_VLAN=$dei_cfg_value ;;
            watcher.enabled) CFG_WATCHER_ENABLED=$dei_cfg_value ;;
            watcher.interval_seconds) CFG_WATCHER_INTERVAL_SECONDS=$dei_cfg_value ;;
            library_patch.enabled) CFG_LIBRARY_PATCH_ENABLED=$dei_cfg_value ;;
            library_patch.policy) CFG_LIBRARY_PATCH_POLICY=$dei_cfg_value ;;
            onu.serial) CFG_ONU_SERIAL=$dei_cfg_value ;;
            onu.omcc_version) CFG_ONU_OMCC_VERSION=$dei_cfg_value ;;
            onu.vendor_id) CFG_ONU_VENDOR_ID=$dei_cfg_value ;;
            onu.equipment_id) CFG_ONU_EQUIPMENT_ID=$dei_cfg_value ;;
            onu.hardware_version) CFG_ONU_HARDWARE_VERSION=$dei_cfg_value ;;
            onu.mib_profile) CFG_ONU_MIB_PROFILE=$dei_cfg_value ;;
            management.ip_address) CFG_MANAGEMENT_IP_ADDRESS=$dei_cfg_value ;;
            management.netmask) CFG_MANAGEMENT_NETMASK=$dei_cfg_value ;;
            management.gateway) CFG_MANAGEMENT_GATEWAY=$dei_cfg_value ;;
            management.ping_ip) CFG_MANAGEMENT_PING_IP=$dei_cfg_value ;;
            management.failsafe_delay) CFG_MANAGEMENT_FAILSAFE_DELAY=$dei_cfg_value ;;
            storage.persistent_root) CFG_STORAGE_PERSISTENT_ROOT=$dei_cfg_value ;;
            *)
                CFG_CONFIG_ERROR="internal parser field mismatch"
                return 1
                ;;
        esac
    done <<EOF
$dei_cfg_parsed
EOF
    CFG_CONFIG_PATH=$dei_cfg_path
    return 0
}
