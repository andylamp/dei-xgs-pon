#!/bin/sh
# Structured installer audit only. Never copy configuration values or raw OMCI logs.
INSTALL_AUDIT_ACTIVE=0
INSTALL_AUDIT_PHASE=preflight
INSTALL_AUDIT_HEADER=DEI-PON-INSTALL-AUDIT-v1

install_audit_recognized() {
    [ -f "$1" ] || return 1
    IFS= read -r install_audit_first_line <"$1" || return 1
    [ "${install_audit_first_line}" = "${INSTALL_AUDIT_HEADER}" ]
}

install_audit_start() {
    INSTALL_AUDIT_FILE="${D:?installer configuration directory is required}/install.log"
    if [ -L "${INSTALL_AUDIT_FILE}" ] || [ -L "${INSTALL_AUDIT_FILE}.old" ]; then
        echo "ERROR: installation audit paths must not be symlinks" >&2
        return 1
    fi
    if [ -e "${INSTALL_AUDIT_FILE}" ]; then
        if ! install_audit_recognized "${INSTALL_AUDIT_FILE}"; then
            echo "ERROR: existing install.log is not this installer's audit log; preserve it before retrying" >&2
            return 1
        fi
        install_audit_size=$(wc -c <"${INSTALL_AUDIT_FILE}") || return 1
        if [ "${install_audit_size}" -gt 131072 ]; then
            if [ -e "${INSTALL_AUDIT_FILE}.old" ] && ! install_audit_recognized "${INSTALL_AUDIT_FILE}.old"; then
                echo "ERROR: unknown previous installation audit; refusing to replace it" >&2
                return 1
            fi
            mv -f "${INSTALL_AUDIT_FILE}" "${INSTALL_AUDIT_FILE}.old" || return 1
        fi
    fi
    if [ ! -e "${INSTALL_AUDIT_FILE}" ]; then
        (
            umask 077
            printf '%s\n' "${INSTALL_AUDIT_HEADER}" >"${INSTALL_AUDIT_FILE}"
        ) || return 1
    fi
    chmod 600 "${INSTALL_AUDIT_FILE}" || return 1
    if [ -f "${INSTALL_AUDIT_FILE}.old" ]; then chmod 600 "${INSTALL_AUDIT_FILE}.old" || return 1; fi
    INSTALL_AUDIT_ACTIVE=1
    install_audit_time=$(date -u '+%Y-%m-%dT%H:%M:%SZ') || install_audit_time=unavailable
    install_audit_note "run action=${ACTION:?installer action is required} time=${install_audit_time}" || return 1
    install_audit_note "selection vlan=${CFG_INTERNET_VLAN:?validated configuration is required} encryption=${CFG_INTERNET_ENCRYPTION_KEY_RING:?validated configuration is required} patch-enabled=${CFG_LIBRARY_PATCH_ENABLED:?validated configuration is required} policy=${CFG_LIBRARY_PATCH_POLICY:?validated configuration is required}" || return 1
    echo "Installation audit: ${INSTALL_AUDIT_FILE}"
}

install_audit_note() {
    [ "${INSTALL_AUDIT_ACTIVE:-0}" = 1 ] || return 0
    printf '%s\n' "$1" >>"${INSTALL_AUDIT_FILE}"
}

install_audit_report() {
    # Input is generated diagnostic output from our own code, never module logs.
    [ "${INSTALL_AUDIT_ACTIVE:-0}" = 1 ] || return 0
    printf 'report %s\n' "$1" >>"${INSTALL_AUDIT_FILE}" &&
        cat "$2" >>"${INSTALL_AUDIT_FILE}"
}

install_audit_finish() {
    [ "${INSTALL_AUDIT_ACTIVE:-0}" = 1 ] || return 0
    if [ "$1" = 0 ]; then
        install_audit_note "result=completed phase=${INSTALL_AUDIT_PHASE}" || return 1
    else
        install_audit_note "result=failed phase=${INSTALL_AUDIT_PHASE} exit=$1" || return 1
    fi
    INSTALL_AUDIT_ACTIVE=0
}
