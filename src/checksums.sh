#!/bin/sh
# Inlined by the installer before it verifies and loads its payloads.
# Preserve the checksum tool's status and validate its first output field.
file_digest() {
    digest_record=$("$1" "$3" 2>/dev/null) || return 1
    digest=${digest_record%% *}
    [ "${#digest}" -eq "$2" ] || return 1
    case "${digest}" in
        *[!0123456789abcdef]*) return 1 ;;
        *) printf '%s\n' "${digest}" ;;
    esac
}
md5() { file_digest md5sum 32 "$1"; }
sha() { file_digest sha256sum 64 "$1"; }
