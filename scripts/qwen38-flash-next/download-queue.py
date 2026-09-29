#!/usr/bin/env python3
"""Mac controller: public HF metadata on Mac, verified payloads through head UID 996."""
import argparse
import fcntl
import importlib.util
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
BASE = "/home/operator/dgx-models/qwen38-flash-next"
REMOTE_PLAN = BASE + "/control/download-plan.json"
SPEC = importlib.util.spec_from_file_location("verify_downloads", HERE / "verify-downloads.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class DownloadError(Exception):
    pass


class Gated(DownloadError):
    pass


def log(message):
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + message, flush=True)


def checked(result, operation):
    # Never include stderr/argv: either might contain a signed URL.
    if result.returncode:
        raise DownloadError(f"{operation} failed (exit {result.returncode})")
    return result.stdout


def validate_signed_url(url):
    u = urllib.parse.urlsplit(url)
    if (u.scheme != "https" or not u.hostname or u.username or u.password
            or u.port not in (None, 443) or any(c.isspace() for c in url)
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", u.hostname)):
        raise DownloadError("invalid signed URL; value suppressed")
    # Only official Hugging Face endpoints/CDNs may serve payloads.
    if not any(u.hostname == d or u.hostname.endswith("." + d)
               for d in ("huggingface.co", "hf.co", "xethub.hf.co")):
        raise DownloadError("unexpected payload CDN; manual inspection required")
    return u.hostname


def validate_metadata(plan, metadata):
    for item in plan["files"]:
        info = metadata[item["repo"]]
        if info.get("sha") != item["revision"]:
            raise DownloadError("upstream revision mismatch")
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
        self.plan = verify.load_plan(args.plan)
        self.files = [(i, f) for i, f in enumerate(self.plan["files"], 1)
                      if not args.small_only or not f["lfs_sha256"]]

    def ssh(self, command, data=None, timeout=1800, check=True):
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
             self.args.host, command], input=data, capture_output=True, timeout=timeout)
        return checked(result, "SSH operation") if check else result

    def http(self, url, head=False, target=None):
        command = ["curl", "-q", "--silent", "--noproxy", "*", "--connect-timeout", "15",
                   "--max-time", "120", "--location", "--max-redirs", "6",
                   "--proto", "=https", "--proto-redir", "=https"]
        if self.args.interface:
            command += ["--interface", self.args.interface]
        command += ["--output", str(target or os.devnull), "--write-out", "%{http_code}\t%{url_effective}"]
        if head:
            command.append("--head")
            # The Mac obtains the official LFS redirect only. CDN payload
            # connections belong to the isolated head identity, not the Mac.
            command.remove("--location")
            command[command.index("%{http_code}\t%{url_effective}")] = "%{http_code}\t%{redirect_url}"
        # URL supplied in config on stdin, never put the signed URL in argv.
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
        with tempfile.TemporaryDirectory(prefix="qwen38-metadata-") as tmp:
            for repo, revision in verify.REVISIONS.items():
                target = Path(tmp) / "metadata.json"
                self.http(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true", target=target)
                output[repo] = json.loads(target.read_text())
        validate_metadata(self.plan, output)
        evidence = PROJECT / "logs/qwen38-flash-next/metadata.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(output, indent=2) + "\n")
        log(f"METADATA_PASS files=29 bytes={verify.TOTAL_BYTES}")

    def preflight(self):
        identity = self.ssh("hostname; whoami", timeout=30).decode().splitlines()
        if identity != ["dgx-head", "operator"]:
            raise DownloadError("head hostname/user identity mismatch")
        self.ssh("test \"$(id -u dgx-hf-direct)\" = 996 && "
                 "sudo -n -u dgx-hf-direct ip route get 1.1.1.1 | grep -F 'dev wlP9s9' >/dev/null")
        self.ssh("ip route get 1.1.1.1 | grep -F 'dev dgx-vpn' >/dev/null")
        self.ssh("command -v aria2c >/dev/null && command -v unshare >/dev/null && "
                 "command -v flock >/dev/null && command -v python3 >/dev/null")
        # No management/fabric/network changes; the target must not hide symlinks.
        self.ssh("python3 -c " + shlex.quote(
            "from pathlib import Path; p=Path(" + repr(BASE) + "); "
            "assert not any(x.is_symlink() for x in [p,*p.parents]); "
            "print('TARGET_PATH_PASS')"))
        log("PREFLIGHT_PASS host=dgx-head user=operator payload_uid=996 routes=isolated")

    def install_control(self):
        self.ssh("python3 -c " + shlex.quote(
            "from pathlib import Path; base=Path(" + repr(BASE) + "); "
            "paths=[base / name for name in "
            "['weights','weights/.download','control','download-logs','download-logs/worker.lock',"
            "'control/verify-downloads.py','control/download-artifact-remote.sh','control/download-plan.json']]; "
            "assert not any(p.is_symlink() for p in paths)"))
        self.ssh(f"sudo -n install -d -o operator -g operator -m 0755 {BASE}/control {BASE}/weights {BASE}/download-logs")
        # Never change a running worker or remove its status during reconnection.
        result = self.ssh(f"sudo -n flock -n {BASE}/download-logs/worker.lock true", check=False)
        if result.returncode:
            raise DownloadError("Qwen38 worker is still active; wait for it before resuming controller")
        for name in ("verify-downloads.py", "download-artifact-remote.sh"):
            self.ssh(f"install -m 0755 /dev/stdin {BASE}/control/{name}", (HERE / name).read_bytes())
        self.ssh(f"install -m 0644 /dev/stdin {REMOTE_PLAN}", Path(self.args.plan).read_bytes())

    def existing(self, item):
        dest = BASE + "/weights/" + item["destination"]
        exists = self.ssh("test -e " + shlex.quote(dest) + " -o -L " + shlex.quote(dest), check=False)
        if exists.returncode == 1:
            return False
        checked(exists, "existing file lookup")
        cmd = ["python3", BASE + "/control/verify-downloads.py", "--file", dest,
               "--size", str(item["size"]), "--digest", verify.file_digest(item)]
        self.ssh(shlex.join(cmd))
        log("SKIP verified_existing=" + item["destination"])
        return True

    def small_blob(self, item):
        with tempfile.TemporaryDirectory(prefix="qwen38-blob-") as tmp:
            # macOS /var is a system symlink; normalize only our own fresh
            # temporary directory, never a user/remote artifact destination.
            local = Path(tmp).resolve() / "blob"
            self.http(self.origin(item), target=local)
            if not verify.verify_file(local, item["size"], verify.file_digest(item)):
                raise DownloadError("Git blob size/hash mismatch: " + item["destination"])
            dest = BASE + "/weights/" + item["destination"]
            # Create beside final, verify on head, atomically promote; interrupted
            # SSH cannot make an incomplete final file. No root is needed here.
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
                                 verify.file_digest(item), BASE + "/control/verify-downloads.py"]), local.read_bytes())

    @staticmethod
    def origin(item):
        path = urllib.parse.quote(item["path"], safe="/")
        return f"https://huggingface.co/{item['repo']}/resolve/{item['revision']}/{path}?download=true"

    def resolve_ipv4(self, hostname):
        # Same isolated direct DoH baseline as prior verified download queues.
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
        artifact = f"qwen38-{index:02}"
        url_file = f"/run/dgx-qwen38-{artifact}.url"
        status_file = f"{BASE}/download-logs/{artifact}.status"
        dest = BASE + "/weights/" + item["destination"]
        for attempt in range(3):
            self.preflight()
            signed = self.http(self.origin(item), head=True)
            hostname = validate_signed_url(signed)
            ip = self.resolve_ipv4(hostname)
            self.ssh(f"sudo -n install -o dgx-hf-direct -g dgx-hf-direct -m 0600 /dev/stdin {url_file}", signed.encode())
            hosts = f"127.0.0.1 localhost\n::1 localhost\n{ip} {hostname}\n"
            self.ssh(f"sudo -n install -o root -g root -m 0644 /dev/stdin {url_file}.hosts", hosts.encode())
            self.ssh(f"sudo -n rm -f {status_file}")
            command = shlex.join(["sudo", "-n", "nohup", "nice", "-n", "15", "ionice", "-c", "3",
                                  BASE + "/control/download-artifact-remote.sh", artifact, url_file,
                                  dest, str(item["size"]), verify.file_digest(item), self.args.limit])
            self.ssh(command + f" >{BASE}/download-logs/{artifact}.log 2>&1 </dev/null &")
            log(f"START payload={artifact} bytes={item['size']} limit={self.args.limit} attempt={attempt + 1}/3")
            # At 32 MiB/s the largest shard should finish in minutes. A 2-hour
            # deadline stops the controller; it never starts a second worker.
            deadline = time.monotonic() + 7200
            start_deadline = time.monotonic() + 60
            worker_observed = False
            while time.monotonic() < deadline:
                result = self.ssh("sudo -n cat " + status_file, check=False)
                if result.returncode and not result.stdout:
                    # A transient SSH failure is not evidence that the worker
                    # never started. Keep the existing worker and bounded wait.
                    if result.returncode == 255 or worker_observed:
                        time.sleep(10)
                        continue
                status = result.stdout.decode().strip()
                if status == "RUNNING":
                    worker_observed = True
                if status == "PASS":
                    self.existing(item)
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
            log(f"PLAN_PASS files=29 bytes={verify.TOTAL_BYTES} selected={len(self.files)}")
            return
        if platform.system() != "Darwin":
            raise DownloadError("network/download controller must run on the Mac")
        if self.args.action == "metadata":
            self.metadata()
            return
        self.preflight()
        if self.args.action == "verify":
            self.install_control()
            command = ["python3", BASE + "/control/verify-downloads.py", "--plan", REMOTE_PLAN, "--root", BASE + "/weights"]
            if self.args.small_only:
                command.append("--small-only")
            result = self.ssh(shlex.join(command), check=False)
            print(result.stdout.decode(), end="", flush=True)
            checked(result, "offline remote verification")
            return
        self.metadata()
        self.install_control()
        for index, item in self.files:
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
    parser.add_argument("--plan", default=str(PROJECT / "plans/qwen38-flash-next/download-plan.json"))
    parser.add_argument("--host", default="operator@192.0.2.30")
    parser.add_argument("--interface", default="en1", help="Mac control network interface; empty uses routing table")
    parser.add_argument("--limit", default="32M", help="aria2 payload bytes/sec limit, e.g. 32M (32 MiB/s)")
    parser.add_argument("--small-only", action="store_true", help="select only ordinary Git blobs, no LFS payloads")
    args = parser.parse_args()
    lock_dir = PROJECT / "logs/qwen38-flash-next"
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (lock_dir / "controller.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DownloadError("another local Qwen38 controller is running")
            Controller(args).run()
    except Gated as exc:
        log("PAUSE " + str(exc))
        return 3
    except DownloadError as exc:
        log("PAUSE " + str(exc))
        return 1
    except Exception as exc:
        # Unexpected subprocess/HTTP errors can embed credentials/URLs.
        log("PAUSE unexpected_error=" + type(exc).__name__ + "; details suppressed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
