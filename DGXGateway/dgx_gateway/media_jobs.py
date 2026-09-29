"""Local persistent preprocessing jobs; they never reserve or load a DGX model."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid

from PIL import Image, ImageOps

from .maintenance import creative_admission


TERMINAL = {"SUCCEEDED", "FAILED", "INTERRUPTED"}


class MediaJobs:
    def __init__(self, app):
        self.app = app
        self.assets = app.creative_jobs.assets
        self.root = app.config.state_dir / "media-jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.active = None
        self._lock = threading.Lock()
        self.npx = Path.home() / ".local/bin/npx"
        for path in self.root.glob("*.json"):
            try:
                row = json.loads(path.read_text())
                if row.get("state") not in TERMINAL:
                    row.update(state="INTERRUPTED", error="Gateway restarted during local media preprocessing")
                    self.save(row)
            except (OSError, ValueError):
                pass

    def save(self, row):
        row["updated_at"] = time.time()
        path = self.root / f"{row['job_id']}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(row, ensure_ascii=False, indent=2))
        temporary.replace(path)

    def get(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("invalid job_id")
        return json.loads((self.root / f"{job_id}.json").read_text())

    def _submit(self, kind, spec, key):
        with self._lock:
            for path in self.root.glob("*.json"):
                old = json.loads(path.read_text())
                if old.get("idempotency_key") == key:
                    if old.get("kind") != kind or old.get("spec") != spec:
                        raise ValueError("idempotency key reused with different media job")
                    return self.get(old["job_id"])
            if self.active:
                raise BlockingIOError("local media preprocessing busy")
            with creative_admission(self.app.config):
                job_id = uuid.uuid4().hex
                row = {"job_id": job_id, "idempotency_key": key, "kind": kind, "spec": spec, "state": "QUEUED", "created_at": time.time()}
                self.save(row)
                self.active = job_id
            threading.Thread(target=self.run, args=(row,), daemon=True, name=f"media-job-{job_id}").start()
            return self.get(job_id)

    def submit_cutout(self, asset_id, key):
        asset = self.assets.get(asset_id)
        if asset["content_type"] not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("unsupported source image")
        return self._submit("CUTOUT", {"asset_id": asset_id}, key)

    def submit_composite(self, background_asset_id, layers, key):
        self.assets.get(background_asset_id)
        if not isinstance(layers, list) or not 1 <= len(layers) <= 16:
            raise ValueError("layers must contain 1–16 items")
        names = set()
        normalized = []
        for index, layer in enumerate(layers):
            required = {"name", "asset_id", "x", "y", "width", "height", "opacity"}
            allowed = required | {"flip_x"}
            if not isinstance(layer, dict) or not required.issubset(layer) or not set(layer).issubset(allowed):
                raise ValueError(f"invalid layer {index}")
            if not isinstance(layer["name"], str) or not layer["name"] or layer["name"] in names:
                raise ValueError(f"invalid layer {index} name")
            names.add(layer["name"])
            self.assets.get(layer["asset_id"])
            normalized_layer = dict(layer)
            normalized_layer["flip_x"] = bool(layer.get("flip_x", False))
            normalized.append(normalized_layer)
        return self._submit("COMPOSITE", {"background_asset_id": background_asset_id, "layers": normalized}, key)

    def _cutout(self, row, temporary):
        if not self.npx.is_file():
            raise RuntimeError("pinned background-removal runtime is unavailable")
        source = self.assets.get(row["spec"]["asset_id"])
        output = temporary / "cutout.png"
        runtime_bin = str(self.npx.parent)
        environment = dict(os.environ)
        environment["PATH"] = runtime_bin + os.pathsep + environment.get("PATH", "")
        result = subprocess.run(
            [str(self.npx), "--yes", "hyperframes@0.8.29", "remove-background", source["path"], "-o", str(output)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=900,
            env=environment,
        )
        if result.returncode or not output.is_file():
            diagnostic = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "no command output"
            raise RuntimeError(f"background removal failed ({result.returncode}): {diagnostic[:300]}")
        with Image.open(output) as image:
            if image.mode != "RGBA" or image.getchannel("A").getextrema() in {(0, 0), (255, 255)}:
                raise RuntimeError("background removal did not produce a usable alpha mask")
        return self.assets.register_base64("cutout.png", base64.b64encode(output.read_bytes()).decode())

    def _composite(self, row, temporary):
        background = self.assets.get(row["spec"]["background_asset_id"])
        with Image.open(background["path"]) as source:
            canvas = source.convert("RGBA")
        for layer in row["spec"]["layers"]:
            asset = self.assets.get(layer["asset_id"])
            with Image.open(asset["path"]) as source:
                item = source.convert("RGBA")
                if item.getchannel("A").getextrema() == (255, 255):
                    raise ValueError(f"layer {layer['name']} has no transparency; call prepare_image_cutout first")
                item = item.resize((layer["width"], layer["height"]), Image.Resampling.LANCZOS)
            if layer.get("flip_x", False):
                item = ImageOps.mirror(item)
            if layer["opacity"] < 100:
                alpha = item.getchannel("A").point(lambda value: value * layer["opacity"] // 100)
                item.putalpha(alpha)
            canvas.alpha_composite(item, (layer["x"], layer["y"]))
        output = temporary / "composite.png"
        canvas.convert("RGB").save(output, format="PNG", optimize=True)
        return self.assets.register_base64("composite.png", base64.b64encode(output.read_bytes()).decode())

    def run(self, row):
        try:
            row["state"] = "RUNNING"
            self.save(row)
            with tempfile.TemporaryDirectory(prefix="dgx-media-", dir=self.app.config.state_dir) as directory:
                temporary = Path(directory)
                row["output_asset"] = self._cutout(row, temporary) if row["kind"] == "CUTOUT" else self._composite(row, temporary)
            row["state"] = "SUCCEEDED"
        except Exception as exc:
            row["state"] = "FAILED"
            row["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        finally:
            self.save(row)
            with self._lock:
                self.active = None
