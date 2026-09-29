"""EasyCache residual/change-rate algorithm adapted to packed H3 AV rows.

Source: Comfy-Org/ComfyUI comfy_extras/nodes_easycache.py. This is a native
Ring4 adaptation, not ComfyUI node execution or Cache-DiT. A request owns its
state. The window is explicit *evaluation progress*, not a raw sigma cutoff,
because PDD's shifted eight-step schedule is highly nonuniform. The first two
and final evaluation always compute. Cache decisions are broadcast by rank0.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time

import torch
import torch.distributed as dist

LOG = logging.getLogger(__name__)


class EasyCache:
    def __init__(self, num_steps, device):
        self.enabled = os.environ.get("H3_EASYCACHE", "0") == "1"
        self.threshold = float(os.environ.get("H3_EASYCACHE_THRESHOLD", "0.2"))
        if not math.isfinite(self.threshold) or not 0 <= self.threshold <= 1:
            raise ValueError("EasyCache threshold must be finite and in [0,1]")
        self.num_steps, self.device = num_steps, device
        self.x_prev = self.out_prev = self.out_norm = self.rate = None
        self.diff_v = self.diff_a = None
        self.accumulated = 0.0
        self.full = self.skipped = 0
        self.started = time.monotonic()

    @staticmethod
    def sample(x):
        return x[::8, ::8].float()

    def skip(self, step, video):
        self.input_change = None
        eligible = self.enabled and 0.15 <= step / self.num_steps < 0.95 and step < self.num_steps - 1
        if eligible and self.x_prev is not None:
            self.input_change = (self.sample(video) - self.x_prev).abs().mean().item()
        should_skip = False
        if eligible and self.rate is not None and self.out_norm and self.input_change is not None:
            estimate = self.rate * self.input_change / self.out_norm
            self.accumulated += estimate
            should_skip = math.isfinite(self.accumulated) and self.accumulated < self.threshold
        if dist.is_available() and dist.is_initialized():
            decision = torch.tensor(int(should_skip), dtype=torch.int32, device=self.device)
            dist.broadcast(decision, src=0)
            should_skip = bool(decision.item())
        if should_skip and (self.diff_v is None or self.diff_a is None):
            raise RuntimeError("EasyCache rank missing required AV residuals")
        self.last_estimate = self.accumulated
        if not should_skip:
            self.accumulated = 0.0
        return should_skip

    def reuse(self, video, audio):
        self.skipped += 1
        # Never modify caller-owned Euler state or the stored residual in place.
        return video + self.diff_v, audio + self.diff_a

    def remember(self, video, audio, out_video, out_audio):
        self.full += 1
        if not self.enabled:
            return
        output = self.sample(out_video)
        if self.out_prev is not None and self.input_change is not None:
            change = (output - self.out_prev).abs().mean().item()
            self.rate = change / self.input_change if self.input_change > 1e-12 else None
        self.x_prev = self.sample(video).clone()
        self.out_prev = output.clone()
        self.out_norm = out_video.float().abs().mean().item()
        self.diff_v = out_video.float() - video
        self.diff_a = out_audio.float() - audio

    def log_step(self, step, skipped, elapsed):
        rank = dist.get_rank() if dist.is_available() and dist.is_initialized() else 0
        LOG.warning("H3_ACCEL_STEP %s", json.dumps({"rank": rank, "step": step + 1,
                    "steps": self.num_steps, "easycache": self.enabled, "skipped": skipped,
                    "threshold": self.threshold, "change_rate": self.last_estimate,
                    "elapsed_seconds": elapsed, "full_calls": self.full, "cache_hits": self.skipped}))

    def finish(self):
        LOG.warning("H3_ACCEL_COMPLETE full_calls=%s cache_hits=%s steps=%s seconds=%.6f",
                    self.full, self.skipped, self.num_steps, time.monotonic() - self.started)
