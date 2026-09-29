"""Pure canvas-draft contract. Executing a node never submits production work."""
from __future__ import annotations

import json

TEMPLATE_VERSION = "dgx-drama-shot-v1"
EDITABLE_TEXT = ("title", "action", "framing", "camera", "prompt")


def build_canvas_draft(*, shotId, expectedRevision, baseRevisionId, templateVersion,
                       title, action, framing, camera, prompt, targetEditFrames,
                       snapshot_json):
    for name, value in (("shotId", shotId), ("baseRevisionId", baseRevisionId)):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{name} must identify an existing server revision")
    if templateVersion != TEMPLATE_VERSION:
        raise ValueError("Unsupported drama canvas template")
    if isinstance(expectedRevision, bool) or not isinstance(expectedRevision, int) or expectedRevision < 1:
        raise ValueError("expectedRevision must be a positive integer")
    if isinstance(targetEditFrames, bool) or not isinstance(targetEditFrames, int) or not 1 <= targetEditFrames <= 100000:
        raise ValueError("targetEditFrames must be an integer from 1 to 100000")
    if not isinstance(snapshot_json, str) or len(snapshot_json.encode("utf-8")) > 512 * 1024:
        raise ValueError("Invalid or oversized snapshot_json")
    snapshot = json.loads(snapshot_json)
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot_json must be an object")
    text = dict(title=title, action=action, framing=framing, camera=camera, prompt=prompt)
    for name, value in text.items():
        if not isinstance(value, str) or len(value) > 20000:
            raise ValueError(f"Invalid {name}")
    if not title.strip():
        raise ValueError("镜头名称不能为空")
    snapshot.update(text, targetEditFrames=targetEditFrames)
    return {"shotId": shotId, "expectedRevision": expectedRevision,
            "baseRevisionId": baseRevisionId, "templateVersion": templateVersion,
            "snapshot": snapshot}
