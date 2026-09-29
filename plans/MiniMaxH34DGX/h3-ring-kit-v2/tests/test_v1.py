"""CPU-only tests: files, HTTP contract handling, and dry-run launch argument assembly."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KIT = Path(__file__).resolve().parents[1]


class LocalTests(unittest.TestCase):
    def test_shell_syntax(self):
        subprocess.run(["bash", "-n", str(KIT / "scripts/run_node.sh")], check=True)

    def test_python_syntax(self):
        for path in (KIT / "scripts").glob("*.py"):
            compile(path.read_text(), str(path), "exec")

    def test_manifest_detects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "model"
            root.mkdir()
            (root / "weights.bin").write_bytes(b"abcd")
            manifest = Path(tmp) / "manifest.json"
            command = [sys.executable, str(KIT / "scripts/manifest.py")]
            subprocess.run(command + ["make", str(root), str(manifest)], check=True, capture_output=True)
            subprocess.run(command + ["verify", str(root), str(manifest)], check=True, capture_output=True)
            (root / "weights.bin").write_bytes(b"abce")
            result = subprocess.run(command + ["verify", str(root), str(manifest)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Manifest mismatch", result.stderr)

    def run_mock_video(self, status):
        posts = []
        content = b"Mock transport bytes; deliberately not a real encoded video."

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data, content_type="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.reply(b'{"id":"job-local-1", "status":"queued"}')

            def do_GET(self):
                if self.path.endswith("/content"):
                    self.reply(content, "video/mp4")
                else:
                    self.reply(json.dumps({"id": "job-local-1", "status": status}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "result.mp4"
                result = subprocess.run([
                    sys.executable, str(KIT / "scripts/video_client.py"),
                    "--base", f"http://127.0.0.1:{server.server_port}",
                    "--request", str(KIT / "requests/smoke.json"),
                    "--out", str(output), "--timeout", "10",
                ], capture_output=True, text=True, timeout=20)
                self.assertEqual(len(posts), 1, "Must not retry job-creating POST")
                if status == "completed":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_bytes(), content)
                    metadata = json.loads(Path(str(output) + ".result.json").read_text())
                    self.assertFalse(metadata["media_decode_verified"])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_mock_http_success(self):
        self.run_mock_video("completed")

    def test_mock_http_failure(self):
        self.run_mock_video("failed")

    def test_mock_http_unknown_state(self):
        self.run_mock_video("unrecognized_future_status")

    def test_ring_peer_mapping(self):
        for world in (2, 4):
            for offset in range(1, world):
                for sender in range(world):
                    receiver = (sender + offset) % world
                    self.assertEqual((receiver - offset) % world, sender)

    def test_socket_launch_is_labeled_and_global_world_is_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            kit = work / "kit"
            (kit / "scripts").mkdir(parents=True)
            shutil.copy2(KIT / "scripts/run_node.sh", kit / "scripts/run_node.sh")
            root = work / "state"
            model = root / "models"
            (model / "FL2VA").mkdir(parents=True)
            (model / "model_index.json").write_text("{}")
            (model / "FL2VA/model_index.json").write_text("{}")
            (kit / "cluster.env").write_text(
                f"H3_ROOT={root}\nIMAGE=local/test\nEXPECTED_IMAGE_ID=sha256:test\n"
                "MASTER_ADDR=192.0.2.5\nDIST_PORT=20021\nTEST_PORT=29521\n"
                f"API_PORT=30021\nAPI_BIND=127.0.0.1\nMODEL_HOST_PATH={model}\n"
                "MODEL_VARIANT=fl2va\nQUANTIZATION=fp8\n"
            )
            (kit / "node.env").write_text("NODE_RANK=0\nCONTROL_IFACE=test0\nNETWORK_MODE=socket\n")
            fake = work / "fakebin"
            fake.mkdir()
            (fake / "ip").write_text("#!/bin/sh\nexit 0\n")
            (fake / "docker").write_text("""#!/bin/sh
if [ "$1" = image ]; then
  case "$*" in *Architecture*) echo arm64;; *) echo sha256:test;; esac
elif [ "$1" = container ]; then
  exit 1
elif [ "$1" = run ]; then
  printf '%s\\n' "$@"
else
  exit 9
fi
""")
            for path in fake.iterdir():
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["bash", str(kit / "scripts/run_node.sh"), "serve", "4"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--num-gpus\n4\n", result.stdout)
            self.assertIn("--ring-degree\n4\n", result.stdout)
            self.assertIn("NCCL_NET=Socket", result.stdout)
            self.assertIn("NCCL_SOCKET_IFNAME==test0", result.stdout)
            self.assertIn("--encoder-parallel\nreplicate", result.stdout)
            self.assertNotIn("--privileged", result.stdout)


if __name__ == "__main__":
    unittest.main()
