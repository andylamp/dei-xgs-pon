#!/bin/sh
# DEI selected-service repair. One background watcher per boot/session.
# Polling interval and behavior come from dei-pon.toml; writes require a verified mismatch.
DEI_DIR=${DEI835_CONF_DIR:-/ptconf/8311}
. "$DEI_DIR/dei-835-lib.sh" || exit 1
case "${1:---watch}" in
    --check|--once|--watch) DEI_MODE=${1:---watch};;
    *) echo "Usage: keyring.sh [--check|--once|--watch]" >&2; exit 2;;
esac

if [ "$DEI_MODE" != --watch ]; then
    dei_discover || { echo "$DEI_ERROR"; exit 1; }
    DEI_FIRST=$DEI_SIGNATURE
    sleep "${DEI835_SETTLE_SECONDS:-1}"
    dei_discover || { echo "$DEI_ERROR"; exit 1; }
    [ "$DEI_FIRST" = "$DEI_SIGNATURE" ] || { echo "Provisioning changed; retry after it settles"; exit 1; }
    echo "VLAN $DEI_VLAN: mapper=$DEI_MAPPER rules=$DEI_RULE GEMs=$DEI_GEMS target-encryption=$DEI_ENCRYPTION normalize=$DEI_NORMALIZE"
    if [ "$DEI_MODE" = --check ]; then
        for dei_gem in $DEI_GEMS; do echo "GEM $dei_gem encryption=$(dei_gem_detail "$dei_gem")"; done
        exit 0
    fi
    mkdir -p "$DEI_RUN" || exit 1
    exec 9>"$DEI_RUN/worker.lock"
    flock -n 9 || { echo "Watcher already running; it will repair any mismatch"; exit 0; }
    DEI_LOGGING=1
    dei_apply
    DEI_RESULT=$?
    dei_flush_log
    exit "$DEI_RESULT"
fi

mkdir -p "$DEI_RUN" || exit 1
exec 9>"$DEI_RUN/worker.lock"
flock -n 9 || exit 0
printf '%s\n' "$$" > "$DEI_RUN/worker.pid"
trap 'rm -f "$DEI_RUN/worker.pid"' EXIT
trap 'exit 0' TERM INT HUP
DEI_LOGGING=1
dei_log "Watcher started; loading dei-pon.toml each polling cycle"
dei_flush_log
DEI_PREVIOUS=; DEI_LAST_REPORT=; DEI_PASSES=0
while :; do
    [ ! -e "$DEI_RUN/stop" ] || { dei_log "Watcher stopped on request"; break; }
    if dei_runtime_config; then
        [ "$DEI_WATCH_ENABLED" = true ] || { dei_log "Watcher disabled by configuration"; break; }
        DEI_CONFIG_READY=1
    else
        DEI_CONFIG_READY=0
    fi
    DEI_FIX_MODE=$(fw_printenv -n 8311_fix_vlans 2>/dev/null)
    case "$DEI_FIX_MODE" in
        0|1) dei_log "Fix VLANs mode changed to $DEI_FIX_MODE; watcher stopped"; break;;
        2) ;;
        *) DEI_ERROR="Cannot confirm hook-only mode; waiting without changes";;
    esac
    if [ "$DEI_CONFIG_READY" = 1 ] && [ "$DEI_FIX_MODE" = 2 ] && dei_discover; then
        if [ "$DEI_SIGNATURE" = "$DEI_PREVIOUS" ]; then
            if dei_apply; then
                DEI_REPORT="Healthy VLAN $DEI_VLAN: $DEI_MAPPER GEMs=$DEI_GEMS"
            else
                DEI_REPORT="Repair incomplete; will retry"
            fi
        else
            DEI_REPORT="Waiting for stable VLAN $DEI_VLAN: $DEI_MAPPER GEMs=$DEI_GEMS"
        fi
        DEI_PREVIOUS=$DEI_SIGNATURE
    else
        DEI_PREVIOUS=
        DEI_REPORT=$DEI_ERROR
    fi
    if [ "$DEI_REPORT" != "$DEI_LAST_REPORT" ]; then
        dei_log "$DEI_REPORT"
        DEI_LAST_REPORT=$DEI_REPORT
    fi
    dei_flush_log
    DEI_PASSES=$((DEI_PASSES + 1))
    # Nonzero limit is for offline simulations; normal operation has no limit.
    if [ "${DEI835_MAX_PASSES:-0}" -gt 0 ] && [ "$DEI_PASSES" -ge "$DEI835_MAX_PASSES" ]; then break; fi
    sleep "$DEI_INTERVAL"
done
dei_flush_log
