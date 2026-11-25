#!/bin/sh
# DEI selected-service repair. Called/sourced by 8311-vlansd in hook-only mode.
# No installation-specific switch_cli writes and no legacy lctfix helper.
(
    exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-
    . "${DEI_PON_CONF_DIR:-/ptconf/8311}/dei-835-lib.sh" || exit 1
    dei_select_config_path
    if [ -z "${dei_config_path}" ] || [ ! -f "${dei_config_path}" ] || [ ! -r "${dei_config_path}" ]; then
        echo 'Firmware hook: selected TOML is missing or unreadable.' >&2
        exit 1
    fi
    rm -f "${DEI_RUN}/stop" || exit 1
    DEI_PON_CONFIG_PATH="${dei_config_path}" sh "${DEI_DIR}/keyring.sh" --watch </dev/null >/dev/null 2>&1 &
)
exit $?
