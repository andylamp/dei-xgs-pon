#!/bin/sh
# DEI selected-service repair: inspect without modifying device configuration.
DEI_DIR=${DEI835_CONF_DIR:-/ptconf/8311}
. "$DEI_DIR/dei-835-lib.sh" || exit 1
pon psg
if dei_discover; then
    dei_report_discovery
    for dei_gem in $DEI_GEMS; do
        pon gpcg "$dei_gem"
    done
else
    dei_report_discovery
fi
dei_pid=$(cat "$DEI_RUN/worker.pid" 2>/dev/null)
case "$dei_pid" in
    '' | *[!0-9]*) echo "Watcher: not running" ;;
    *) if kill -0 "$dei_pid" 2>/dev/null && tr '\000' ' ' <"/proc/$dei_pid/cmdline" | grep -q 'keyring.sh --watch'; then
        echo "Watcher: running (PID $dei_pid)"
    else echo "Watcher: not running (stale PID file)"; fi ;;
esac
if [ -f "$DEI_DIR/dei835.log" ]; then
    echo "Runtime repair log: $DEI_DIR/dei835.log"
    tail -n 12 "$DEI_DIR/dei835.log"
fi
if [ -f "$DEI_DIR/install.log" ]; then
    IFS= read -r dei_audit_header <"$DEI_DIR/install.log" || dei_audit_header=
    if [ "$dei_audit_header" = DEI-PON-INSTALL-AUDIT-v1 ]; then
        echo "Latest installation audit: $DEI_DIR/install.log"
        tail -n 12 "$DEI_DIR/install.log"
    else
        echo "Installation log has an unrecognized format; contents were not displayed."
    fi
fi
