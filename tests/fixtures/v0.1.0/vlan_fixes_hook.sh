#!/bin/sh
# DEI selected-service repair. Called/sourced by 8311-vlansd in hook-only mode.
# No installation-specific switch_cli writes and no legacy lctfix helper.
rm -f /tmp/dei835/stop
(
    exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-
    DEI835_CONFIG_PATH=/ptconf/8311/dei-pon.toml sh /ptconf/8311/keyring.sh --watch </dev/null >/dev/null 2>&1 &
)
exit 0
