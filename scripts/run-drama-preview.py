#!/usr/bin/env python3
"""Isolated workbench preview using the production drama handler/service.

This server exposes only drama routes and owns a separate state directory. It
does not construct model adapters, admission controllers or lifecycle services.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "DGXGateway"))

from dgx_gateway.drama.routes import handle
from dgx_gateway.drama.service import DramaService
from dgx_gateway.server import GatewayApplication, GatewayHandler
from http.server import ThreadingHTTPServer


class PreviewHandler(GatewayHandler):
    def dispatch(self):
        if not handle(self):
            self.send_error(404, "Preview exposes only drama routes")

    do_GET = do_POST = do_HEAD = dispatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18084)
    parser.add_argument("--gateway-state-dir", type=Path, help="Enable image tasks through the existing fixed production Gateway")
    parser.add_argument("--runtime-config", type=Path, help="Explicit saved local postproduction runtime settings")
    args = parser.parse_args()
    if args.runtime_config:
        values = json.loads(args.runtime_config.read_text())
        allowed = {"DGX_DRAMA_FFMPEG", "DGX_DRAMA_FFPROBE", "DGX_DRAMA_ASR_WORKER", "DGX_DRAMA_ASR_MODEL", "DGX_DRAMA_FONT_FILE", "DGX_DRAMA_FONT_FAMILY", "DGX_DRAMA_FONT_PROBE"}
        if not isinstance(values, dict) or set(values) - allowed or any(not isinstance(v,str) for v in values.values()): raise ValueError("Invalid postproduction runtime settings")
        for key, value in values.items():
            if key != "DGX_DRAMA_FONT_FAMILY" and (not Path(value).is_absolute() or not Path(value).is_file()): raise ValueError("Postproduction runtime path unavailable: " + key)
            os.environ[key] = value
        # Legacy upload validation resolves ``ffmpeg``/``ffprobe`` from PATH,
        # while postproduction uses the explicit runtime paths above. Keep the
        # isolated preview consistent without depending on the launch shell's
        # (often minimal launchd) PATH.
        media_dirs = [str(Path(values[key]).parent) for key in
                      ("DGX_DRAMA_FFMPEG", "DGX_DRAMA_FFPROBE") if values.get(key)]
        inherited = os.environ.get("PATH", "").split(os.pathsep)
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys(media_dirs + inherited))
    config = SimpleNamespace(lan_no_auth=True, allowed_origins=[f"http://127.0.0.1:{args.port}"])
    app = GatewayApplication(config=config, store=None, operations=None, snapshot=None,
                             static_dir=ROOT / "DGXGateway/static", admission=None)
    adapter = None
    video_adapter = None
    text_adapter = None
    if args.gateway_state_dir:
        from dgx_gateway.drama.gateway_adapter import GatewayReferenceAdapter
        adapter = GatewayReferenceAdapter(state_dir=args.gateway_state_dir.resolve())
        from dgx_gateway.drama.gateway_video_adapter import GatewayVideoAdapter
        video_adapter = GatewayVideoAdapter(state_dir=args.gateway_state_dir.resolve())
        from dgx_gateway.drama.gateway_text_adapter import GatewayTextAdapter
        text_adapter = GatewayTextAdapter()
    app.drama = DramaService(args.state_dir.resolve(), ROOT, adapter, video_adapter, text_adapter)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    server.application = app
    import signal
    import threading
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"Drama preview ready on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        app.drama.close()
        server.server_close()


if __name__ == "__main__":
    main()
