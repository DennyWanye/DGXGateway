#!/usr/bin/env python3
"""Mac controller: public HF metadata on Mac, verified payloads through head UID 996."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import tempfile
import time
import urllib.parse

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
BASE = "/home/operator/dgx-models/dsv41-native-shared"
STOCK = BASE + "/checkpoint-fb2764a"
OVERLAY = BASE + "/assets/native-ablit-overlay"
REMOTE_PLAN = BASE + "/audit/checkpoint/download-plan.json"
ENGRAM = {"model-00047-of-00048.safetensors", "model-00048-of-00048.safetensors"}
STOCK_REPO = "deepseek-ai/DeepSeek-V4.1-Flash"
STOCK_REV = "fb2764a5cf321eaa5070ca8f9e892818f477c16d"


class DownloadError(Exception):
    pass


class Gated(DownloadError):
    pass


def log(message):
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + message, flush=True)


def checked(result, operation):
    if result.returncode:
        raise DownloadError(f"{operation} failed (exit {result.returncode})")
    return result.stdout


def file_digest(item):
    return item["lfs_sha256"] or "gitsha1:" + item["git_blob_id"]


def validate_signed_url(url):
    u = urllib.parse.urlsplit(url)
    if (u.scheme != "https" or not u.hostname or u.username or u.password
            or u.port not in (None, 443) or any(c.isspace() for c in url)
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", u.hostname)):
        raise DownloadError("invalid signed URL; value suppressed")
    if not any(u.hostname == d or u.hostname.endswith("." + d)
               for d in ("huggingface.co", "hf.co", "xethub.hf.co")):
        raise DownloadError("unexpected payload CDN; manual inspection required")
    return u.hostname


def load_plan(path):
    plan = json.loads(Path(path).read_text())
    files = plan["files"]
    if plan.get("revision") != STOCK_REV or plan.get("repo") != STOCK_REPO:
        raise ValueError("unexpected pinned repo/revision")
    seen = set()
    total = 0
    for item in files:
        path, dest = item["path"], item["destination"]
        if item["repo"] != STOCK_REPO or item["revision"] != STOCK_REV:
            raise ValueError("unapproved repo/revision")
        if (not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
                or Path(path).is_absolute() or ".." in Path(path).parts
                or dest != path or dest in seen):
            raise ValueError("invalid or duplicate destination")
        if not isinstance(item["size"], int) or item["size"] <= 0:
            raise ValueError("invalid expected size")
        if not re.fullmatch(r"[0-9a-f]{40}", item["git_blob_id"]):
            raise ValueError("invalid Git blob identity")
        sha = item["lfs_sha256"]
        if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("invalid LFS digest")
        if sha is None and item["size"] > 64 * 1024 * 1024:
            raise ValueError("ordinary Git blob exceeds small-file limit")
        seen.add(dest)
        total += item["size"]
    if total != plan["total_bytes"]:
        raise ValueError("file sizes do not match total")
    return plan


def validate_metadata(plan, metadata):
    info = metadata[plan["repo"]]
    if info.get("sha") != plan["revision"]:
        raise DownloadError("upstream revision mismatch")
    for item in plan["files"]:
        rows = [s for s in info["siblings"] if s["rfilename"] == item["path"]]
        if len(rows) != 1:
            raise DownloadError("missing or duplicate upstream artifact")
        row = rows[0]
        lfs = row.get("lfs") or {}
        if (lfs.get("size", row.get("size")) != item["size"]
                or row.get("blobId") != item["git_blob_id"]
                or lfs.get("sha256") != item["lfs_sha256"]):
            raise DownloadError("upstream metadata differs from pinned plan: " + item["destination"])


class Controller:
    def __init__(self, args):
        self.args = args
        if not re.fullmatch(r"[1-9][0-9]*[KM]?", args.limit):
            raise DownloadError("invalid download limit; use e.g. 32M")
        self.plan = load_plan(args.plan)
        self.files = [(i, f) for i, f in enumerate(self.plan["files"], 1)
                      if not args.small_only or not f["lfs_sha256"]]

    def ssh(self, command, data=None, timeout=1800, check=True):
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
             self.args.host, command], input=data, capture_output=True, timeout=timeout)
        return checked(result, "SSH operation") if check else result

    def dest(self, item):
        return STOCK + "/" + item["destination"]

    def http(self, url, head=False, target=None):
        command = ["curl", "-q", "--silent", "--noproxy", "*", "--connect-timeout", "15",
                   "--max-time", "120", "--location", "--max-redirs", "6",
                   "--proto", "=https", "--proto-redir", "=https"]
        if self.args.interface:
            command += ["--interface", self.args.interface]
        command += ["--output", str(target or os.devnull), "--write-out", "%{http_code}\t%{url_effective}"]
        if head:
            command.append("--head")
            command.remove("--location")
            command[command.index("%{http_code}\t%{url_effective}")] = "%{http_code}\t%{redirect_url}"
        command += ["--config", "-"]
        config = "url = " + json.dumps(url) + "\n"
        for attempt in range(3):
            try:
                result = subprocess.run(command, input=config.encode(), capture_output=True, timeout=130)
            except subprocess.TimeoutExpired:
                result = subprocess.CompletedProcess([], 28, b"000\t", b"")
            status, _, effective = result.stdout.decode().partition("\t")
            if status in ("401", "403"):
                raise Gated("HTTP_401_403; credentials are never read or transmitted")
            if head and result.returncode == 0 and status in ("302", "303", "307", "308"):
                validate_signed_url(effective)
                return effective
            if not head and result.returncode == 0 and status in ("200", "206"):
                return effective
            log(f"RETRY control_http attempt={attempt + 1}/3 http={status if status.isdigit() else 'unknown'}")
            if attempt < 2:
                time.sleep(10)
        raise DownloadError("control HTTP exhausted three attempts")

    def metadata(self):
        output = {}
        with tempfile.TemporaryDirectory(prefix="dsv41n-metadata-") as tmp:
            target = Path(tmp) / "metadata.json"
            self.http(f"https://huggingface.co/api/models/{STOCK_REPO}/revision/{STOCK_REV}?blobs=true",
                      target=target)
            output[STOCK_REPO] = json.loads(target.read_text())
        validate_metadata(self.plan, output)
        evidence = PROJECT / "logs/dsv41-native-prefetch/metadata.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(output, indent=2) + "\n")
        log(f"METADATA_PASS files={len(self.plan['files'])} bytes={self.plan['total_bytes']}")

    def preflight(self):
        identity = self.ssh("hostname; whoami", timeout=30).decode().splitlines()
        if identity != ["dgx-head", "operator"]:
            raise DownloadError("head hostname/user identity mismatch")
        self.ssh("test \"$(id -u dgx-hf-direct)\" = 996 && "
                 "sudo -n -u dgx-hf-direct ip route get 1.1.1.1 | grep -F 'dev wlP9s9' >/dev/null")
        self.ssh("ip route get 1.1.1.1 | grep -F 'dev dgx-vpn' >/dev/null")
        self.ssh("command -v aria2c >/dev/null && command -v unshare >/dev/null && "
                 "command -v flock >/dev/null && command -v python3 >/dev/null")
        self.ssh("python3 -c " + shlex.quote(
            "from pathlib import Path; p=Path(" + repr(BASE) + "); "
            "assert not any(x.is_symlink() for x in [p,*p.parents]); "
            "print('TARGET_PATH_PASS')"))
        log("PREFLIGHT_PASS host=dgx-head user=operator payload_uid=996 routes=isolated")

    def install_control(self):
        self.ssh(f"sudo -n install -d -o operator -g operator -m 0755 {BASE}/tools {STOCK} {BASE}/download-logs {BASE}/audit/checkpoint")
        result = self.ssh(f"sudo -n flock -n {BASE}/download-logs/worker.lock true", check=False)
        if result.returncode:
            raise DownloadError("native stock worker is still active; wait before resuming controller")
        self.ssh(f"install -m 0755 /dev/stdin {BASE}/tools/verify-file.py",
                 (HERE / "verify-file.py").read_bytes())
        self.ssh(f"install -m 0755 /dev/stdin {BASE}/tools/download-artifact-remote.sh",
                 (HERE / "download-artifact-remote.sh").read_bytes())
        self.ssh(f"install -m 0644 /dev/stdin {REMOTE_PLAN}", Path(self.args.plan).read_bytes())

    def engram_row(self, item):
        receipt = BASE + "/audit/checkpoint/engram-reuse-hash.json"
        script = (
            "import json,sys; from pathlib import Path\n"
            "p=Path(sys.argv[1])\n"
            "if not p.is_file(): raise SystemExit(2)\n"
            "row=next((r for r in json.loads(p.read_text()) if r['name']==sys.argv[2]), None)\n"
            "print(json.dumps(row or {}))\n"
        )
        result = self.ssh(shlex.join(["python3", "-c", script, receipt, item["path"]]), check=False)
        if result.returncode == 2 or not result.stdout.strip():
            return None
        checked(result, "engram receipt")
        return json.loads(result.stdout.decode())

    def hardlink_engram(self, item, row):
        src = "/home/operator/dgx-models/mia-deepseek-v41-ablit/engram-slim/" + item["path"]
        dest = self.dest(item)
        script = (
            "import os,sys\n"
            "from pathlib import Path\n"
            "src, dest, inode, device, size = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])\n"
            "st=src.stat()\n"
            "assert st.st_ino==inode and st.st_dev==device and st.st_size==size\n"
            "assert not any(p.is_symlink() for p in [src,dest,*dest.parents])\n"
            "dest.parent.mkdir(parents=True, exist_ok=True)\n"
            "if dest.exists():\n"
            "    ds=dest.stat(); assert ds.st_ino==inode and ds.st_dev==device and ds.st_size==size\n"
            "else:\n"
            "    os.link(src, dest)\n"
            "print('HARDLINK')\n"
        )
        self.ssh(shlex.join(["python3", "-c", script, src, dest, str(row["inode"]), str(row["device"]), str(item["size"])]))
        log("HARDLINK engram=" + item["destination"])

    def reused(self, item):
        if item["path"] not in ENGRAM:
            return False
        row = self.engram_row(item)
        if not row or not row.get("hash_ok"):
            return False
        dest = self.dest(item)
        exists = self.ssh("test -f " + shlex.quote(dest) + " -a ! -L " + shlex.quote(dest), check=False)
        if exists.returncode:
            self.hardlink_engram(item, row)
        log("SKIP reused_engram=" + item["destination"])
        return True

    def existing(self, item):
        dest = self.dest(item)
        exists = self.ssh("test -e " + shlex.quote(dest) + " -o -L " + shlex.quote(dest), check=False)
        if exists.returncode == 1:
            return False
        checked(exists, "existing file lookup")
        if self.reused(item):
            return True
        cmd = ["python3", BASE + "/tools/verify-file.py", "--file", dest,
               "--size", str(item["size"]), "--digest", file_digest(item)]
        self.ssh(shlex.join(["nice", "-n", "15", "ionice", "-c", "3"] + cmd), timeout=14400)
        log("SKIP verified_existing=" + item["destination"])
        return True

    def origin(self, item):
        path = urllib.parse.quote(item["path"], safe="/")
        return f"https://huggingface.co/{item['repo']}/resolve/{item['revision']}/{path}?download=true"

    def small_blob(self, item):
        with tempfile.TemporaryDirectory(prefix="dsv41n-blob-") as tmp:
            local = Path(tmp).resolve() / "blob"
            self.http(self.origin(item), target=local)
            digest = file_digest(item)
            check = subprocess.run(["python3", str(HERE / "verify-file.py"), "--file", str(local),
                                    "--size", str(item["size"]), "--digest", digest])
            if check.returncode:
                raise DownloadError("Git blob size/hash mismatch: " + item["destination"])
            dest = self.dest(item)
            script = """import os, pathlib, subprocess, sys, tempfile
target=pathlib.Path(sys.argv[1]); size=sys.argv[2]; digest=sys.argv[3]
assert not any(p.is_symlink() for p in [target,*target.parents])
target.parent.mkdir(parents=True,exist_ok=True)
fd, partial=tempfile.mkstemp(prefix='.'+target.name+'.',suffix='.part',dir=target.parent)
try:
    with os.fdopen(fd,'wb') as out:
        out.write(sys.stdin.buffer.read()); out.flush(); os.fsync(out.fileno())
    subprocess.run(['python3',sys.argv[4],'--file',partial,'--size',size,'--digest',digest],check=True)
    if target.exists(): raise RuntimeError('destination appeared during transfer')
    os.chmod(partial,0o644); os.link(partial,target); os.unlink(partial)
finally:
    if os.path.exists(partial): os.unlink(partial)
"""
            self.ssh(shlex.join(["python3", "-c", script, dest, str(item["size"]),
                                 digest, BASE + "/tools/verify-file.py"]), local.read_bytes())

    def resolve_ipv4(self, hostname):
        url = "https://doh.pub/dns-query?name=" + hostname + "&type=A"
        command = shlex.join(["sudo", "-n", "runuser", "-u", "dgx-hf-direct", "--",
                              "curl", "-q", "--noproxy", "*", "-4", "--fail", "--silent", "--max-time", "30",
                              "--resolve", "doh.pub:443:1.12.12.12", url, "-H", "accept: application/dns-json"])
        for attempt in range(3):
            result = self.ssh(command, timeout=45, check=False)
            if result.returncode == 0:
                try:
                    rows = json.loads(result.stdout).get("Answer", [])
                    for row in rows:
                        if row.get("type") != 1:
                            continue
                        ip = ipaddress.ip_address(row["data"])
                        if ip.version == 4 and ip.is_global and ip not in ipaddress.ip_network("198.18.0.0/15"):
                            return str(ip)
                except (ValueError, KeyError):
                    pass
            if attempt < 2:
                time.sleep(10)
        raise DownloadError("public DNS exhausted three attempts")

    def payload(self, index, item):
        artifact = f"dsv41n-{index:03}"
        url_file = f"/run/dgx-dsv41n-{artifact}.url"
        status_file = f"{BASE}/download-logs/{artifact}.status"
        dest = self.dest(item)
        for attempt in range(3):
            self.preflight()
            signed = self.http(self.origin(item), head=True)
            hostname = validate_signed_url(signed)
            ip = self.resolve_ipv4(hostname)
            self.ssh(f"sudo -n install -o dgx-hf-direct -g dgx-hf-direct -m 0600 /dev/stdin {url_file}", signed.encode())
            hosts = f"127.0.0.1 localhost\n::1 localhost\n{ip} {hostname}\n"
            self.ssh(f"sudo -n install -o root -g root -m 0644 /dev/stdin {url_file}.hosts", hosts.encode())
            self.ssh(f"sudo -n rm -f {status_file}")
            command = shlex.join(["sudo", "-n", "systemd-run", "--quiet", "--collect", "--unit", artifact + "-download",
                                  "-p", "MemoryMax=512M", "-p", "MemorySwapMax=0", "-p", "CPUQuota=25%",
                                  "-p", "Nice=15", "-p", "IOSchedulingClass=idle",
                                  BASE + "/tools/download-artifact-remote.sh", artifact, url_file,
                                  dest, str(item["size"]), item["lfs_sha256"], self.args.limit])
            self.ssh(command)
            log(f"START payload={artifact} bytes={item['size']} limit={self.args.limit} attempt={attempt + 1}/3")
            deadline = time.monotonic() + 86400
            start_deadline = time.monotonic() + 60
            worker_observed = False
            status = ""
            while time.monotonic() < deadline:
                result = self.ssh("sudo -n cat " + status_file, check=False)
                if result.returncode and not result.stdout:
                    if result.returncode == 255 or worker_observed:
                        time.sleep(10)
                        continue
                status = result.stdout.decode().strip()
                if status == "RUNNING":
                    worker_observed = True
                if status == "PASS":
                    log("VERIFIED payload=" + artifact)
                    return
                if status.startswith("BLOCKED:"):
                    raise Gated("payload HTTP_401_403; queue paused")
                if status.startswith("FAIL:"):
                    if status == "FAIL:4":
                        raise DownloadError("completed staging hash mismatch; inspect before retry")
                    break
                if status not in ("", "RUNNING"):
                    raise DownloadError("unrecognized worker status")
                if not status and not worker_observed and time.monotonic() > start_deadline:
                    raise DownloadError("worker did not start; inspect remote log")
                time.sleep(10)
            else:
                raise DownloadError("worker deadline exceeded; inspect before resuming")
            log(f"RETRY payload={artifact} status={status}")
        raise DownloadError("payload exhausted three attempts: " + artifact)

    def run(self):
        if self.args.action == "validate-plan":
            log(f"PLAN_PASS files={len(self.plan['files'])} bytes={self.plan['total_bytes']} selected={len(self.files)}")
            return
        if platform.system() != "Darwin":
            raise DownloadError("network/download controller must run on the Mac")
        if self.args.action == "metadata":
            self.metadata()
            return
        self.preflight()
        if self.args.action == "verify":
            self.install_control()
            raise DownloadError("use remote prefetch_native.py --offline-verify after the queue finishes")
        self.metadata()
        self.install_control()
        for index, item in self.files:
            if item["path"] in ENGRAM and not self.reused(item):
                receipt = self.ssh("test -s " + BASE + "/audit/checkpoint/engram-reuse-hash.json", check=False)
                if receipt.returncode:
                    log("WAIT engram_hash name=" + item["path"])
                    continue
                if self.reused(item):
                    continue
            if self.existing(item):
                continue
            log("START artifact=" + item["destination"])
            if item["lfs_sha256"]:
                self.payload(index, item)
            else:
                self.small_blob(item)
            log("PASS artifact=" + item["destination"])
        log(f"QUEUE_COMPLETE selected={len(self.files)} small_only={self.args.small_only}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate-plan", "metadata", "download", "verify"))
    parser.add_argument("--plan", default=str(HERE / "download-plan.json"))
    parser.add_argument("--host", default="dgx-head")
    parser.add_argument("--interface", default="en1")
    parser.add_argument("--limit", default="32M")
    parser.add_argument("--small-only", action="store_true")
    args = parser.parse_args()
    lock_dir = PROJECT / "logs/dsv41-native-prefetch"
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (lock_dir / "controller.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DownloadError("another local native stock controller is running")
            Controller(args).run()
    except Gated as exc:
        log("PAUSE " + str(exc))
        return 3
    except DownloadError as exc:
        log("PAUSE " + str(exc))
        return 1
    except Exception:
        log("PAUSE unexpected_error=" + type(exc).__name__ + "; details suppressed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
