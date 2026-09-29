#!/bin/sh
set -eu

SOURCE_SECRET=/run/secrets-source/h3_gateway_token
SOURCE_CONTROL_SECRET=/run/secrets-source/h3_profile_control_token
PRIVATE_DIR=/run/dgx-h3-secret
PRIVATE_SECRET="$PRIVATE_DIR/token"
PRIVATE_CONTROL_SECRET="$PRIVATE_DIR/control-token"
APP_UID=65532
APP_GID=65532
EXPECTED_INITIAL_CAPS=00000000000001c1

fail() {
    printf '%s\n' "container-init: $1" >&2
    exit 78
}

for forbidden_name in H3_URL DGX_H3_URL DGX_H3_ENDPOINT DGX_H3_ENDPOINT_ID DGX_H3_PROFILE COMFYUI_H3_URL; do
    eval "forbidden_is_set=\${${forbidden_name}+yes}"
    [ "${forbidden_is_set:-}" != yes ] || fail "production endpoint override is forbidden"
done

[ "$(id -u)" = 0 ] || fail "initial process must be uid 0"
[ "$(id -g)" = 0 ] || fail "initial process must be gid 0"

status_value() {
    awk -v key="$1:" '$1 == key { print $2 }' /proc/self/status
}

[ "$(status_value NoNewPrivs)" = 1 ] || fail "no-new-privileges is required"
[ "$(status_value CapInh)" = 0000000000000000 ] || fail "unexpected inheritable capabilities"
[ "$(status_value CapAmb)" = 0000000000000000 ] || fail "unexpected ambient capabilities"
for cap_field in CapPrm CapEff CapBnd; do
    [ "$(status_value "$cap_field")" = "$EXPECTED_INITIAL_CAPS" ] || fail "initial capability set is not exact"
done

[ -d "$PRIVATE_DIR" ] || fail "private tmpfs is missing"
[ ! -L "$SOURCE_SECRET" ] || fail "source secret must not be a symlink"

# The Python block deliberately uses O_NOFOLLOW/O_EXCL, os.fsync, chmod and
# chown so the privileged operation order remains reviewable and testable.
python3 - "$SOURCE_SECRET" "$SOURCE_CONTROL_SECRET" "$PRIVATE_DIR" "$PRIVATE_SECRET" "$PRIVATE_CONTROL_SECRET" "$APP_UID" "$APP_GID" <<'PY'
import os
import stat
import sys

source, control_source, private_dir, target, control_target, uid_text, gid_text = sys.argv[1:]
uid, gid = int(uid_text), int(gid_text)

def fail(message: str) -> None:
    print(f"container-init: {message}", file=sys.stderr)
    raise SystemExit(78)

for current_source, current_target in ((source, target), (control_source, control_target)):
    try:
        source_stat = os.lstat(current_source)
    except FileNotFoundError:
        fail("source secret is missing")
    if not stat.S_ISREG(source_stat.st_mode):
        fail("source secret must be a regular file")
    if source_stat.st_uid != 0 or source_stat.st_gid != 0:
        fail("source secret owner must be root")
    if stat.S_IMODE(source_stat.st_mode) & 0o077:
        fail("source secret permissions are not private")
    with open(current_source, "rb", buffering=0) as stream:
        raw = stream.read(4097)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not (16 <= len(raw) <= 4096) or b"\x00" in raw or b"\n" in raw or b"\r" in raw:
        fail("source secret content is invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(current_target, flags, 0o600)
    except FileExistsError:
        fail("private secret target already exists")
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(current_target, 0o400, follow_symlinks=False)
    os.chown(current_target, uid, gid, follow_symlinks=False)

# Validated order: chmod/chown both files, then chown the mode-0700 directory.
os.chown(private_dir, uid, gid, follow_symlinks=False)
PY

export DGX_H3_SOURCE_SECRET="$SOURCE_SECRET"
export DGX_H3_PRIVATE_SECRET="$PRIVATE_SECRET"
export DGX_H3_CONTROL_SECRET="$PRIVATE_CONTROL_SECRET"
export DGX_H3_SOURCE_CONTROL_SECRET="$SOURCE_CONTROL_SECRET"

exec setpriv \
    --reuid=65532 \
    --regid=65532 \
    --clear-groups \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    /opt/dgx/container/steady-state.sh "$@"
