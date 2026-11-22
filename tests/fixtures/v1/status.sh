#!/bin/sh
# DEI VLAN 835 repair v1: inspect state without modifying device configuration.
DEI_DIR=${DEI835_CONF_DIR:-/ptconf/8311}
. "$DEI_DIR/dei-835-lib.sh" || exit 1
pon psg
if dei_discover; then
    echo "VLAN 835: mapper=$DEI_MAPPER rules=$DEI_RULE GEMs=$DEI_GEMS"
    for dei_gem in $DEI_GEMS; do
        echo "GEM $dei_gem driver encryption=$(dei_gem_detail "$dei_gem")"
        pon gpcg "$dei_gem"
    done
else
    echo "$DEI_ERROR"
fi
dei_pid=$(cat "$DEI_RUN/worker.pid" 2>/dev/null)
case "$dei_pid" in
    ''|*[!0-9]*) echo "Watcher: not running";;
    *) if kill -0 "$dei_pid" 2>/dev/null && tr '\000' ' ' < "/proc/$dei_pid/cmdline" | grep -q 'keyring.sh --watch'; then
           echo "Watcher: running (PID $dei_pid)"
       else echo "Watcher: not running (stale PID file)"; fi;;
esac
tail -n 12 "$DEI_DIR/dei835.log" 2>/dev/null
