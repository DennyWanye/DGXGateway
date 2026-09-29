#!/usr/bin/env bash
# Download-only image archive, deliberately never contacts the Docker daemon.
set -euo pipefail
base=/home/operator/dgx-models/mia-deepseek-v41-download
[[ $(hostname) == dgx-head && $(id -u) == 0 && $(id -u dgx-hf-direct) == 996 ]]
status="$base/download-logs/image.status"
exec 9>"$base/download-logs/image.lock"
flock -n 9 || exit 75
finish() {
 rc=$?
 if ((rc != 0)); then printf 'FAIL:%s\n' "$rc" >"$status"; fi
}
trap finish EXIT
printf 'WAITING_FOR_MODELS\n' >"$status"
while ! python3 - "$base" <<'PY'
import json,sys,pathlib
p=pathlib.Path(sys.argv[1]);plan=json.loads((p/'control/download-plan.json').read_text())
for f in plan['files']:
 t=p/'weights'/f['destination']
 if t.is_symlink() or not t.is_file() or t.stat().st_size!=f['size']: raise SystemExit(1)
PY
do sleep 60; done
runuser -u dgx-hf-direct -- ip route get 1.1.1.1 | grep -F 'dev wlP9s9' >/dev/null
runuser -u operator -- ip route get 1.1.1.1 | grep -F 'dev dgx-vpn' >/dev/null
python3 - "$base" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])/'image'
assert not any(x.is_symlink() for x in [p,*p.parents])
PY
install -d -o dgx-hf-direct -g dgx-hf-direct -m 0755 "$base/image"
image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image"])' "$base/materials/image-lock.json")
# Enforce the locked archive source; no mutable tag and no credentials supplied.
[[ $image == ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks@sha256:2f0cf3adc0f989c1d446be274df864eb799630175f604c3b22b71b7205971dce ]]
printf 'DOWNLOADING\n' >"$status"
umask 077
logfile="$base/control/image-private.log"
if ! unshare --mount --propagation private bash -c '
 set -euo pipefail
 mount --bind "$1" /etc/hosts
 exec runuser -u dgx-hf-direct -- env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy skopeo --command-timeout 6h copy --retry-times 2 --override-os linux --override-arch arm64 "docker://$2" "dir:$3"
' bash "$base/control/image.hosts" "$image" "$base/image" >"$logfile" 2>&1; then
 if grep -Eiq '401|403|unauthorized|denied' "$logfile"; then echo 'registry access error; inspect permissions' >&2; fi
 exit 1
fi
printf 'VERIFYING\n' >"$status"
python3 - "$base" <<'PY'
import sys,pathlib,json,hashlib,os
p=pathlib.Path(sys.argv[1]);root=p/'image'
raw=(root/'manifest.json').read_bytes();lock=json.loads((p/'materials/image-lock.json').read_text())
assert 'sha256:'+hashlib.sha256(raw).hexdigest()==lock['manifest_digest']
m=json.loads(raw)
for item in [m['config'],*m['layers']]:
 f=root/item['digest'].split(':')[1]
 assert f.is_file() and not f.is_symlink() and f.stat().st_size==item['size']
 h=hashlib.sha256()
 with f.open('rb') as src:
  for b in iter(lambda:src.read(1024*1024),b''):
   h.update(b)
   os.posix_fadvise(src.fileno(),src.tell()-len(b),len(b),os.POSIX_FADV_DONTNEED)
 assert 'sha256:'+h.hexdigest()==item['digest']
c=json.loads((root/m['config']['digest'].split(':')[1]).read_text())
assert c['architecture']=='arm64' and c['os']=='linux'
(root/'VERIFIED.json').write_text(json.dumps({'image':lock['image'],'layers':len(m['layers']),'architecture':'arm64','os':'linux'})+'\n')
PY
rm -f "$logfile"
printf 'PASS\n' >"$status"
