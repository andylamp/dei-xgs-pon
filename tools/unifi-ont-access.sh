#!/bin/sh
# Run on the UniFi gateway. The ONT management network uses the physical SFP+ port.
set -eu

interface=eth9
address=192.168.11.2/24
destination=192.168.11.0/24

# Reading a missing interface fails the service so systemd can retry at boot.
addresses=$(ip -o -4 addr show dev "${interface}")
present=$(printf '%s\n' "${addresses}" | awk -v wanted="${address}" '
    $3 == "inet" && $4 == wanted { print "yes"; exit }')
if [ "${present}" != yes ]; then
    ip addr add dev "${interface}" local "${address}"
fi

# Keep the rule specification identical for the existence check and append.
set -- POSTROUTING -o "${interface}" -d "${destination}" -j SNAT --to-source "${address%/*}"
if iptables -w 5 -t nat -C "$@"; then
    exit 0
else
    status=$?
fi
# A missing rule returns 1. Propagate other failures (such as lock contention).
[ "${status}" -eq 1 ] || exit "${status}"
iptables -w 5 -t nat -A "$@"
