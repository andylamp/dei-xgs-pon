#!/bin/sh
# DEI selected-service repair: inspect without modifying device configuration.
DEI_DIR=${DEI_PON_CONF_DIR:-${DEI835_CONF_DIR:-/ptconf/8311}}
. "${DEI_DIR}/dei-835-lib.sh" || exit 1
dei_status_result=0
dei_read_pon psg || {
    echo 'PON status unavailable: the bounded command failed.' >&2
    dei_status_result=1
}
if dei_discover; then
    dei_report_discovery
    for dei_gem in ${DEI_GEMS}; do
        if dei_counter=$(dei_read_pon gpcg "${dei_gem}"); then
            printf '%s\n' "${dei_counter}"
            if ! dei_counter_code=$(printf '%s\n' "${dei_counter}" | dei_omci_error_code) || [ "${dei_counter_code}" != 0 ]; then
                echo "GEM ${dei_gem} counters unavailable: the vendor response failed." >&2
                dei_status_result=1
            fi
        else
            echo "GEM ${dei_gem} counters unavailable: the bounded command failed." >&2
            dei_status_result=1
        fi
    done
else
    dei_report_discovery
    dei_status_result=1
fi
dei_pid=$(cat "${DEI_RUN}/worker.pid" 2>/dev/null)
case "${dei_pid}" in
    '' | *[!0-9]*) echo "Watcher: not running" ;;
    *) if kill -0 "${dei_pid}" 2>/dev/null && tr '\000' ' ' <"/proc/${dei_pid}/cmdline" | grep -q 'keyring.sh --watch'; then
        echo "Watcher: running (PID ${dei_pid})"
    else echo "Watcher: not running (stale PID file)"; fi ;;
esac
if [ -L "${DEI_DIR}/dei835.log" ] || { [ -e "${DEI_DIR}/dei835.log" ] && [ ! -f "${DEI_DIR}/dei835.log" ]; }; then
    echo 'Runtime log is not a regular managed file; contents were not displayed.'
    dei_status_result=1
elif [ -f "${DEI_DIR}/dei835.log" ]; then
    echo "Runtime repair log: ${DEI_DIR}/dei835.log"
    tail -n 12 "${DEI_DIR}/dei835.log"
fi
if [ -L "${DEI_DIR}/install.log" ] || { [ -e "${DEI_DIR}/install.log" ] && [ ! -f "${DEI_DIR}/install.log" ]; }; then
    echo 'Installation log is not a regular managed file; contents were not displayed.'
    dei_status_result=1
elif [ -f "${DEI_DIR}/install.log" ]; then
    IFS= read -r dei_audit_header <"${DEI_DIR}/install.log" || dei_audit_header=
    if [ "${dei_audit_header}" = DEI-PON-INSTALL-AUDIT-v1 ]; then
        echo "Latest installation audit: ${DEI_DIR}/install.log"
        tail -n 12 "${DEI_DIR}/install.log"
    else
        echo "Installation log has an unrecognized format; contents were not displayed."
    fi
fi
exit "${dei_status_result}"
