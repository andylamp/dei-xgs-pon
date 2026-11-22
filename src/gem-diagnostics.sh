#!/bin/sh
# Installer-only, read-only GEM counter and encryption checks for diagnose.
set -u

# pontop prints one hardware table per invocation. Bound the runtime and output
# size and retain the exit status before parsing it. Only the installer-owned
# staging directory receives output. Never launch a host tool for a fake module.
md_read_gem_table() (
    md_tool=$(command -v pontop) || return 1
    case "${md_tool}" in /*) ;; *) return 1 ;; esac
    if [ -n "${DEI835_MODULE_ROOT:-}" ]; then
        md_root=$(readlink -f "${DEI835_MODULE_ROOT}") && [ -n "${md_root}" ] && [ "${md_root}" != / ] || return 1
        md_path=$(readlink -f "${md_tool}") || return 1
        case "${md_path}" in "${md_root}"/*) ;; *) return 1 ;; esac
    fi
    ulimit -f 256 || return 1
    exec timeout -s KILL 5 "${md_tool}" -b -g 'GEM/XGEM Port Counters' 7>&- 8>&- 9>&-
)

md_gem_rows() {
    # Field 2 is the Port-ID, distinct from field 1 (hardware table index).
    LC_ALL=C awk -v members="$1" '
        BEGIN {n=split(members,ids," "); for(i=1;i<=n;i++) wanted[ids[i]]=1}
        NF {
            $1=$1
            if(++line==1) {if($0!="Page: GEM/XGEM Port Counters") exit 1; next}
            if(line==2) {
                if($0!="GEM Index GEM ID u/s packets u/s bytes d/s packets d/s bytes Key Errors") exit 1
                next
            }
            if(NF!=7) exit 1
            for(i=1;i<=7;i++) if($i !~ /^(0|[1-9][0-9]*)$/ || length($i)>20) exit 1
            if(index_seen[$1]++ || port_seen[$2]++) exit 1
            if($2 in wanted) printf "%s %s %s %s\n",$2,$1,$5,$7
        }
        END {if(line<3) exit 1}' "$2"
}

md_gem_deltas() {
    # Decimal subtraction keeps 64-bit packet counters exact on BusyBox awk.
    LC_ALL=C awk -v members="$1" '
        function less(a,b) {return length(a)!=length(b) ? length(a)<length(b) : ("x" a)<("x" b)}
        function delta(a,b,  i,j,borrow,digit,out) {
            j=length(b)
            for(i=length(a); i>0; i--) {
                digit=substr(a,i,1)-(j>0 ? substr(b,j,1) : 0)-borrow
                borrow=(digit<0); out=(digit+10*borrow) out
                j--
            }
            sub(/^0+/,"",out); return length(out) ? out : "0"
        }
        FILENAME==ARGV[1] {idx[$1]=$2; rx[$1]=$3; errs[$1]=$4; next}
        {idx2[$1]=$2; rx2[$1]=$3; errs2[$1]=$4}
        END {
            n=split(members,ids," ")
            for(i=1;i<=n;i++) {
                g=ids[i]
                if(!(g in idx) || !(g in idx2)) {print g,"- - missing"; continue}
                if(idx[g]!=idx2[g] || less(rx2[g],rx[g]) || less(errs2[g],errs[g])) {
                    print g,"- - discontinuity"; continue
                }
                packets=delta(rx2[g],rx[g]); errors=delta(errs2[g],errs[g])
                if(less(packets,errors)) result="inconsistent"
                else if(errors!="0") result="rising"
                else if(packets=="0") result="idle"
                else if(errs2[g]!="0") result="historical"
                else result="clean"
                print g,packets,errors,result
            }
        }' "$2" "$3"
}

md_same_service() {
    dei_discover && [ "${DEI_SIGNATURE:-}" = "$1" ]
}

md_gem_ring() {
    # meadg is a read. Never timeout or kill the firmware's OMCI wrapper.
    if ! md_same_service "$2" || ! dei_verify_me_port "$1" || ! md_same_service "$2"; then
        echo "GEM $1: OMCI key-ring unavailable (service or class-268 Port-ID could not be verified)."
        return
    fi
    if md_reply=$(omci_pipe.sh meadg 268 "$1" 10 7>&- 8>&- 9>&- 2>/dev/null) &&
        md_ring=$(printf '%s\n' "${md_reply}" | awk '
            NF {n++; if(NF==2 && $1=="errorcode=0" && $2~/^attr_data=0[0-3]$/) ring=substr($2,12)}
            END {if(n!=1 || ring=="") exit 1; print ring}') &&
        md_same_service "$2" && md_driver=$(dei_gem_detail "$1") && [ -n "${md_driver}" ]; then
        if [ "${md_ring}" != "${md_driver}" ]; then
            md_finding='OMCI and driver disagree'
        elif [ "${md_ring}" != "${DEI_ENCRYPTION:?}" ]; then
            md_finding='configured target differs'
        else
            md_finding='settings match despite rising key errors'
        fi
        echo "GEM $1: OMCI key-ring=${md_ring} driver=${md_driver} target=${DEI_ENCRYPTION} (${md_finding})."
    else
        echo "GEM $1: OMCI key-ring unavailable (unrecognized reply or changed service)."
    fi
}

module_gem_snapshot() {
    # Load the verified bundled helpers with the same staged TOML as --check.
    export DEI_PON_CONFIG_PATH="${S}/config.toml"
    . "${S}/dei-835-lib.sh" || return 1
    if ! dei_discover; then
        echo 'GEM counters unavailable: selected service discovery is incomplete.'
        return 1
    fi
    md_identity=${DEI_SIGNATURE:?}
    if [ "$1" = last ]; then
        md_initial=$(cat "${S}/gem-window.identity") || return 1
        if [ "${md_initial}" != "${md_identity}" ]; then
            echo 'GEM counters inconclusive: selected service changed between samples.'
            return 1
        fi
    fi
    if ! md_read_gem_table >"${S}/gem-window.raw" 2>/dev/null ||
        ! md_gem_rows "${DEI_GEMS:?}" "${S}/gem-window.raw" >"${S}/gem-window.$1"; then
        echo 'GEM counters unavailable: pontop failed or returned an unrecognized counter table.'
        return 1
    fi
    if ! md_same_service "${md_identity}"; then
        echo 'GEM counters inconclusive: selected service changed while reading counters.'
        return 1
    fi
    if [ "$1" = first ]; then
        printf '%s\n' "${md_identity}" >"${S}/gem-window.identity"
        return $?
    fi
    if ! md_gem_deltas "${DEI_GEMS}" "${S}/gem-window.first" "${S}/gem-window.last" >"${S}/gem-window.delta"; then
        echo 'GEM counters unavailable: counter comparison failed.'
        return 1
    fi
    while read -r md_port md_rx md_errors md_state; do
        case "${md_state}" in
            missing)
                echo "GEM ${md_port}: counters unavailable (missing row)."
                continue
                ;;
            discontinuity)
                echo "GEM ${md_port}: counters inconclusive (reset, wrap or reassigned index)."
                continue
                ;;
            *) ;;
        esac
        printf 'GEM %s: downstream-delta=%s key-error-delta=%s ' "${md_port}" "${md_rx}" "${md_errors}"
        case "${md_state}" in
            rising)
                echo '(rising key errors).'
                md_gem_ring "${md_port}" "${md_identity}"
                ;;
            idle) echo '(no downstream traffic observed).' ;;
            historical) echo '(historical key errors; no new errors with downstream traffic).' ;;
            clean) echo '(no key errors with downstream traffic).' ;;
            *) echo '(inconsistent counter deltas; inconclusive).' ;;
        esac
    done <"${S}/gem-window.delta"
}

case "$#:${1:-}" in
    1:first | 1:last) ;;
    *)
        echo 'Expected one GEM snapshot phase (first or last).' >&2
        exit 2
        ;;
esac
S=${DEI_PON_CONF_DIR:-${DEI835_CONF_DIR:?private installer staging directory required}}
module_gem_snapshot "$1"
