#!/bin/sh
# DEI VLAN 835 repair v1. Called/sourced by 8311-vlansd in hook-only mode.
# No installation-specific switch_cli writes and no legacy lctfix helper.
rm -f /tmp/dei835/stop
(
    exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-
    sh /ptconf/8311/keyring.sh --watch </dev/null >/dev/null 2>&1 &
)
exit 0
