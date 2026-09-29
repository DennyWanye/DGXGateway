#!/bin/sh
set -eu

fail() {
    printf '%s\n' "container-steady: $1" >&2
    exit 78
}

[ "$(id -u)" = 65532 ] || fail "steady uid is not 65532"
[ "$(id -g)" = 65532 ] || fail "steady gid is not 65532"
[ "$(id -G)" = 65532 ] || fail "unexpected supplementary groups"

status_value() {
    awk -v key="$1:" '$1 == key { print $2 }' /proc/self/status
}

for cap_field in CapInh CapPrm CapEff CapBnd CapAmb; do
    [ "$(status_value "$cap_field")" = 0000000000000000 ] || fail "$cap_field is not zero"
done
[ "$(status_value NoNewPrivs)" = 1 ] || fail "NoNewPrivs is not one"

[ ! -r "$DGX_H3_SOURCE_SECRET" ] || fail "source secret remains readable"
[ ! -r "$DGX_H3_SOURCE_CONTROL_SECRET" ] || fail "source control secret remains readable"
[ -r "$DGX_H3_PRIVATE_SECRET" ] || fail "private secret is not readable"
[ ! -L "$DGX_H3_PRIVATE_SECRET" ] || fail "private secret is a symlink"
[ "$(stat -c '%u:%g:%a:%F' "$DGX_H3_PRIVATE_SECRET")" = "65532:65532:400:regular file" ] || fail "private secret metadata is invalid"
[ -r "$DGX_H3_CONTROL_SECRET" ] || fail "private control secret is not readable"
[ ! -L "$DGX_H3_CONTROL_SECRET" ] || fail "private control secret is a symlink"
[ "$(stat -c '%u:%g:%a:%F' "$DGX_H3_CONTROL_SECRET")" = "65532:65532:400:regular file" ] || fail "private control secret metadata is invalid"

probe=/.dgx-h3-readonly-probe
if (umask 077; : >"$probe") 2>/dev/null; then
    rm -f "$probe" 2>/dev/null || true
    fail "root filesystem is writable"
fi

if [ "$#" -eq 0 ]; then
    set -- python3 /opt/ComfyUI/main.py \
        --cpu \
        --listen 0.0.0.0 \
        --port 8188 \
        --disable-api-nodes \
        --disable-all-custom-nodes \
        --whitelist-custom-nodes dgx_remote_h3 \
        --input-directory /data/input \
        --output-directory /data/output \
        --user-directory /data/user \
        --temp-directory /data/temp
fi

exec "$@"
