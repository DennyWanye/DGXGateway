"""P1 contract: durable editing, idempotent imports, frozen frame exports."""
import http.client
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.server import GatewayHTTPServer


class DramaServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.source = Path(cls.fixtures.name) / "two-shots.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4.5", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.source)], check=True)
        cls.data = cls.source.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = DramaService(self.root, self.root)
        self.pid = self.service.create_project({"title": "片段测试", "width": 96, "height": 64, "requestKey": "project"})["project"]["id"]
        self.eid = self.service.create_episode(self.pid, {"title": "第一集", "requestKey": "episode"})["episode"]["id"]

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def asset(self, key="upload"):
        return self.service.upload(self.pid, io.BytesIO(self.data), len(self.data), "源片.mp4", key)["asset"]

    def shot(self, aid, key="shot"):
        shot = self.service.create_shot(self.eid, {"title": key, "assetId": aid, "requestKey": key})["shot"]
        return self.service.select_take(shot["id"], {"takeId": shot["takes"][0]["id"], "expectedRevision": 1, "requestKey": "select-" + key})["shot"]

    def timeline(self, takes, lengths=None, expected=0, key="timeline"):
        clips = [{"clipId": f"clip-{i}", "takeId": take, "inFrame": 0, "frames": length,
                  "keepSourceAudio": i == 0, "fit": "contain"}
                 for i, (take, length) in enumerate(zip(takes, lengths or [96] * len(takes)))]
        return self.service.save_timeline(self.eid, {"clips": clips, "expectedRevision": expected, "requestKey": key})["timeline"]

    def wait_export(self, xid):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            row = self.service.export(xid)["export"]
            if row["state"] in {"SUCCEEDED", "FAILED", "INTERRUPTED"}:
                return row
            time.sleep(.02)
        self.fail("export did not finish")

    def test_same_key_is_idempotent_and_conflicting_input_rejected(self):
        body = {"title": "one", "requestKey": "same"}
        first = self.service.create_project(body)
        self.assertEqual(first, self.service.create_project(body))
        with self.assertRaises(DramaError) as error:
            self.service.create_project({**body, "title": "two"})
        self.assertEqual(error.exception.status, 409)

    def test_import_deduplicates_and_rejects_invalid_or_truncated(self):
        self.assertEqual(self.asset(), self.asset())
        self.assertEqual(len(self.service.project(self.pid)["assets"]), 1)
        with self.assertRaises(DramaError):
            self.service.upload(self.pid, io.BytesIO(b"not video"), 9, "x.mp4", "bad")
        with self.assertRaises(DramaError):
            self.service.upload(self.pid, io.BytesIO(self.data[:10]), len(self.data), "x.mp4", "short")
        self.assertFalse(list((self.service.root / "temp").iterdir()))

    def test_source_library_does_not_accept_paths_or_symlinks(self):
        directory = self.root / "creative-projects/demo/video"
        directory.mkdir(parents=True)
        (directory / "source.mp4").write_bytes(self.data)
        (directory / "link.mp4").symlink_to(self.source)
        sources = self.service.library()["sources"]
        self.assertEqual([s["title"] for s in sources], ["source.mp4"])
        result = self.service.import_source(self.pid, {"sourceId": sources[0]["sourceId"], "requestKey": "library"})
        self.assertEqual(result["asset"]["frames"], 108)
        with self.assertRaises(DramaError):
            self.service.import_source(self.pid, {"sourceId": "/etc/passwd", "requestKey": "bad"})

    def test_project_boundary_and_wrong_shot_selection(self):
        aid = self.asset()["id"]
        other = self.service.create_project({"title": "other", "requestKey": "other"})["project"]["id"]
        with self.assertRaises(DramaError):
            self.service.media_path(other, aid)
        a, b = self.shot(aid, "a"), self.shot(aid, "b")
        with self.assertRaises(DramaError):
            self.service.select_take(a["id"], {"takeId": b["selectedTakeId"], "expectedRevision": 2, "requestKey": "wrong"})

    def test_restart_preserves_selection_timeline_and_order(self):
        aid = self.asset()["id"]
        a, b = self.shot(aid, "a"), self.shot(aid, "b")
        timeline = self.timeline([a["selectedTakeId"], b["selectedTakeId"]])
        episode = self.service.episode(self.eid)["episode"]
        self.service.reorder(self.eid, {"shotIds": [b["id"], a["id"]], "expectedRevision": episode["revision"], "requestKey": "reorder"})
        self.service.close()
        self.service = DramaService(self.root, self.root)
        restored = self.service.episode(self.eid)
        self.assertEqual(restored["timeline"], timeline)
        self.assertEqual([s["id"] for s in restored["shots"]], [b["id"], a["id"]])
        self.assertTrue(all(s["selectedTakeId"] for s in restored["shots"]))

    def test_stale_revision_and_out_of_bounds_timeline_rejected(self):
        shot = self.shot(self.asset()["id"])
        with self.assertRaises(DramaError) as error:
            self.service.select_take(shot["id"], {"takeId": shot["selectedTakeId"], "expectedRevision": 1, "requestKey": "stale"})
        self.assertEqual(error.exception.status, 409)
        with self.assertRaises(DramaError):
            self.timeline([shot["selectedTakeId"]], [109])
        first = self.timeline([shot["selectedTakeId"]])
        with self.assertRaises(DramaError):
            self.timeline([shot["selectedTakeId"]], expected=0, key="stale-timeline")
        self.assertEqual(self.service.episode(self.eid)["timeline"], first)

    def test_export_freezes_input_and_can_repeat_same_asset(self):
        shot = self.shot(self.asset()["id"])
        timeline = self.timeline([shot["selectedTakeId"]] * 2)
        from DGXGateway.dgx_gateway.drama.media import render_timeline
        entered, proceed = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            if not proceed.wait(10):
                raise RuntimeError("test timeout")
            return render_timeline(*args, **kwargs)
        with patch("DGXGateway.dgx_gateway.drama.media.render_timeline", side_effect=delayed):
            item = self.service.create_export(timeline["id"], {"requestKey": "export"})["export"]
            self.assertTrue(entered.wait(10))
            duplicate = self.service.create_export(timeline["id"], {"requestKey": "export"})["export"]
            self.assertEqual(item["id"], duplicate["id"])
            self.timeline([shot["selectedTakeId"]], [24], expected=1, key="new-timeline")
            proceed.set()
            result = self.wait_export(item["id"])
        self.assertEqual(result["state"], "SUCCEEDED", result)
        asset = next(a for a in self.service.project(self.pid)["assets"] if a["id"] == result["assetId"])
        self.assertEqual(asset["frames"], 192)
        self.assertAlmostEqual(asset["duration"], 8, places=3)

    def test_interrupted_exports_are_not_replayed_on_restart(self):
        shot = self.shot(self.asset()["id"])
        tl = self.timeline([shot["selectedTakeId"]] * 2)
        with patch.object(self.service._executor, "submit") as submit:
            row = self.service.create_export(tl["id"], {"requestKey": "queued"})["export"]
            submit.assert_called_once()
        self.service.close()
        self.service = DramaService(self.root, self.root)
        self.assertEqual(self.service.export(row["id"])["export"]["state"], "INTERRUPTED")
        old = self.service.create_export(tl["id"], {"requestKey": "queued"})["export"]
        self.assertEqual(old["state"], "INTERRUPTED")
        newer = self.timeline([shot["selectedTakeId"]], [24], expected=1, key="newer")
        new = self.service.create_export(tl["id"], {"requestKey": "explicit-retry"})["export"]
        self.assertNotEqual(old["id"], new["id"])
        result = self.wait_export(new["id"])
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual(result["timelineId"], tl["id"])
        self.assertEqual(self.service.episode(self.eid)["timeline"]["id"], newer["id"])
        asset = next(a for a in self.service.project(self.pid)["assets"] if a["id"] == result["assetId"])
        self.assertEqual(asset["frames"], 192, "retry must use original 192 frames, not current 24")

    def test_missing_and_modified_assets_fail_without_overwrite(self):
        aid = self.asset()["id"]
        shot = self.shot(aid)
        tl = self.timeline([shot["selectedTakeId"]])
        path = self.service.media_path(self.pid, aid)
        path.write_bytes(b"damaged")
        row = self.service.create_export(tl["id"], {"requestKey": "damaged"})["export"]
        self.assertEqual(self.wait_export(row["id"])["state"], "FAILED")
        self.assertEqual(path.read_bytes(), b"damaged")
        path.unlink()
        with self.assertRaises(DramaError) as error:
            self.service.media_path(self.pid, aid)
        self.assertEqual(error.exception.status, 404)


class DramaHTTPTests(DramaServiceTests):
    def setUp(self):
        super().setUp()
        config = SimpleNamespace(lan_no_auth=True, allowed_origins=("http://localhost",))
        self.app = SimpleNamespace(drama=self.service, config=config, authorized=lambda cookie: True)
        self.server = GatewayHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        connection.request(method, "/api/v1/drama" + path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_binary_upload_range_head_and_forbidden_origin(self):
        code, _, body = self.request("POST", f"/projects/{self.pid}/assets", self.data,
                                     {"Content-Type": "application/octet-stream", "X-Filename": "demo.mp4", "X-Request-Key": "http-upload"})
        self.assertEqual(code, 201, body)
        aid = json.loads(body)["asset"]["id"]
        path = f"/projects/{self.pid}/assets/{aid}/media"
        code, headers, content = self.request("GET", path, headers={"Range": "bytes=0-1023"})
        self.assertEqual((code, len(content), content), (206, 1024, self.data[:1024]))
        self.assertEqual(headers["Content-Range"], f"bytes 0-1023/{len(self.data)}")
        code, headers, content = self.request("HEAD", path)
        self.assertEqual((code, headers["Content-Length"], content), (200, str(len(self.data)), b""))
        self.assertEqual(self.request("GET", path, headers={"Range": "bytes=99999999-"})[0], 416)
        self.assertEqual(self.request("GET", path, headers={"Range": "bytes=-10"})[2], self.data[-10:])
        code, _, _ = self.request("POST", "/projects", b'{}', {"Origin": "https://untrusted.test", "Content-Type": "application/json"})
        self.assertEqual(code, 403)

    def test_disabled_returns_capability_but_no_business_route(self):
        del self.app.drama
        self.assertEqual(json.loads(self.request("GET", "/capabilities")[2])["enabled"], False)
        self.assertEqual(self.request("GET", "/projects")[0], 404)


if __name__ == "__main__":
    unittest.main()
