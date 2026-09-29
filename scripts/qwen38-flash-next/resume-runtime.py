#!/usr/bin/env python3
"""Mac metadata controller: retain verified OCI layers and acquire missing blobs."""
import hashlib
import json
from pathlib import Path
import shlex
import ssl
import subprocess
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "plans/qwen38-flash-next/evidence/deploy"
REMOTE = "/home/operator/dgx-models/qwen38-flash-next/oci/base-pinned"
PIN = "3b0e188ffceb3d07e09c3cb5215433a0020eacf02d7f882ed3a8bfd15454477e"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "operator@192.0.2.30"]
TLS = ssl.create_default_context(cafile="/etc/ssl/cert.pem")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def blob_source(digest, size):
    with urllib.request.urlopen("https://auth.docker.io/token?service=registry.docker.io&scope=repository:vllm/vllm-openai:pull", context=TLS, timeout=25) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request("https://registry-1.docker.io/v2/vllm/vllm-openai/blobs/" + digest,
                                     headers={"Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=TLS))
    try:
        with opener.open(request, timeout=25) as response:
            if size > 100_000:
                raise RuntimeError("large blob did not provide a CDN redirect")
            raw = response.read(size + 1)
            assert len(raw) == size and hashlib.sha256(raw).hexdigest() == digest.split(":")[1]
            return {"hex": raw.hex()}
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308):
            raise RuntimeError("registry HTTP " + str(error.code) + "; hard pause") from None
        url = error.headers["Location"]
        assert url.startswith("https://") and "\n" not in url
        return {"url": url}


REMOTE_SCRIPT = r'''
import hashlib,json,pathlib,subprocess,sys,os
p=json.load(sys.stdin);root=pathlib.Path(p['root']);name=p['digest'].split(':')[1]
assert len(name)==64 and all(c in '0123456789abcdef' for c in name)
assert root.is_dir() and not any(x.is_symlink() for x in (root,*root.parents))
target=root/name;part=root/(name+'.part')
if target.exists():
 with target.open('rb') as f: sha=hashlib.file_digest(f,'sha256').hexdigest()
 assert target.stat().st_size==p['size'] and sha==name,'existing canonical blob is corrupt; preserve for inspection'
 print(json.dumps({'digest':p['digest'],'status':'ALREADY_VERIFIED'}));sys.exit(0)
assert not part.is_symlink()
if 'hex' in p:
 assert not part.exists()
 part.write_bytes(bytes.fromhex(p['hex']))
else:
 # Build real newlines; the URL never appears in argv or emitted diagnostics.
 config='url = '+json.dumps(p['url'])+chr(10)
 args=['curl','--config','-','--proxy','socks5h://127.0.0.1:31081','--location','--fail',
       '--silent','--show-error','--continue-at','-','--connect-timeout','25','--max-time','1200',
       '--retry','2','--retry-delay','3','--output',str(part),'--write-out','%{http_code} %{size_download} %{speed_download}']
 r=subprocess.run(args,input=config,text=True,capture_output=True,timeout=3700)
 print(json.dumps({'digest':p['digest'],'curl_exit':r.returncode,'metrics':r.stdout.strip(),'stderr_bytes':len(r.stderr)}),flush=True)
 if r.returncode: sys.exit(r.returncode)
assert part.stat().st_size==p['size'],'download size mismatch'
with part.open('rb') as f: sha=hashlib.file_digest(f,'sha256').hexdigest()
assert sha==name,'download digest mismatch'
os.chmod(part,0o644);part.rename(target)
print(json.dumps({'digest':p['digest'],'status':'VERIFIED','bytes':p['size']}),flush=True)
'''


def main():
    raw = (EVIDENCE / "runtime-manifest.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PIN
    inventory = json.loads((EVIDENCE / "runtime-resume-inventory.json").read_text())
    expected = json.loads(raw)
    assert {x["digest"] for x in inventory["files"]} == {x["digest"] for x in [expected["config"], *expected["layers"]]}
    for row in inventory["files"]:
        if row["verified"]:
            continue
        print(json.dumps({"event": "ACQUIRING", "digest": row["digest"], "bytes": row["size"]}), flush=True)
        source = blob_source(row["digest"], row["size"])
        payload = {"root": REMOTE, "digest": row["digest"], "size": row["size"], **source}
        command = "sudo -n -u dgx-hf-direct python3 -c " + shlex.quote(REMOTE_SCRIPT)
        result = subprocess.run(SSH + [command], input=json.dumps(payload), text=True, capture_output=True, timeout=3800)
        print(result.stdout, end="", flush=True)
        if result.returncode:
            raise RuntimeError("blob acquisition failed; exit=" + str(result.returncode) + "; retained partial; diagnostics suppressed")
    # The manifest is the completion marker consumed by the waiting offline builder.
    publish = "import sys,pathlib,hashlib; r=sys.stdin.buffer.read(); assert hashlib.sha256(r).hexdigest()=='" + PIN + "'; p=pathlib.Path('" + REMOTE + "/manifest.json'); t=p.with_suffix('.part'); assert not p.exists() and not t.exists(); t.write_bytes(r); t.replace(p)"
    subprocess.run(SSH + ["sudo -n -u dgx-hf-direct python3 -c " + shlex.quote(publish)], input=raw, check=True, timeout=30)
    print("ALL_BLOBS_VERIFIED_MANIFEST_PUBLISHED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # HTTP and subprocess exceptions can contain signed URLs. Do not print them.
        print(json.dumps({"status": "PAUSED", "error_type": type(error).__name__}), flush=True)
        raise SystemExit(1)
