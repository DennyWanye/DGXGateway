#!/usr/bin/env bash
set -euo pipefail

base_url=${1:-http://127.0.0.1:18082/v1}
model=${MODEL:?set MODEL}
work=$(mktemp -d)
trap 'find "$work" -type f -delete; rmdir "$work"' EXIT

curl --fail --silent --show-error "$base_url/models" >"$work/models.json"
python3 - "$work/models.json" "$model" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert any(item.get("id") == sys.argv[2] for item in data["data"]), data
PY

curl --fail --silent --show-error "$base_url/responses" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$model\",\"instructions\":\"Reply exactly CODEX_COMPAT_OK.\",\"input\":\"Return the required marker.\",\"reasoning\":{\"effort\":\"none\"},\"max_output_tokens\":64}" \
  >"$work/nonstream.json"
python3 - "$work/nonstream.json" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
text = "".join(
    part.get("text", "")
    for item in data.get("output", [])
    for part in item.get("content", [])
    if part.get("type") == "output_text"
)
assert data.get("status") == "completed", data
assert "CODEX_COMPAT_OK" in text, data
PY

curl --fail --silent --show-error --no-buffer "$base_url/responses" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$model\",\"input\":\"Reply exactly STREAM_COMPAT_OK.\",\"reasoning\":{\"effort\":\"none\"},\"stream\":true,\"max_output_tokens\":64}" \
  >"$work/stream.sse"
grep -F 'response.completed' "$work/stream.sse"
grep -F 'STREAM_COMPAT_OK' "$work/stream.sse"

curl --fail --silent --show-error "$base_url/responses" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$model\",\"input\":\"Call report_probe exactly once with marker CODEX_TOOL_COMPAT. Do not answer in prose.\",\"tools\":[{\"type\":\"function\",\"name\":\"report_probe\",\"description\":\"Report a test marker\",\"parameters\":{\"type\":\"object\",\"properties\":{\"marker\":{\"type\":\"string\"}},\"required\":[\"marker\"],\"additionalProperties\":false}}],\"tool_choice\":\"required\",\"reasoning\":{\"effort\":\"none\"},\"max_output_tokens\":128}" \
  >"$work/tool.json"
python3 - "$work/tool.json" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
calls = [item for item in data.get("output", []) if item.get("type") == "function_call"]
assert len(calls) == 1, data
assert calls[0].get("name") == "report_probe", data
assert json.loads(calls[0]["arguments"]) == {"marker": "CODEX_TOOL_COMPAT"}, data
PY

echo "Codex Responses compatibility: PASS model=$model"
