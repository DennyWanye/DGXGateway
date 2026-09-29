#!/usr/bin/env python3
"""Call the LAN Gateway MCP; JSON arguments, no credentials needed."""
import argparse
import base64
import json
from pathlib import Path
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('tool', nargs='?')
parser.add_argument('--drama', action='store_true', help='Use the current local workbench business MCP')
parser.add_argument('--list', action='store_true', help='List tools on the selected endpoint')
parser.add_argument('arguments', nargs='?', default='{}')
parser.add_argument('--arguments-file', type=Path, help='Read JSON from file, useful for base64 image inputs')
parser.add_argument('--image-file', type=Path, help='Register this local image without creating a base64 request file')
parser.add_argument('--audio-file', type=Path, help='Register this local four-second WAV without creating a base64 request file')
args = parser.parse_args()
if not args.tool and not args.list:
    parser.error('provide a tool name or --list')
if args.drama and (args.image_file or args.audio_file):
    parser.error('media registration flags belong to model Gateway, not drama MCP')
selected_files = sum(value is not None for value in (args.arguments_file, args.image_file, args.audio_file))
if selected_files > 1:
    parser.error('--arguments-file, --image-file and --audio-file are mutually exclusive')
if args.image_file:
    if args.tool != 'comfy_register_image':
        parser.error('--image-file is only valid with comfy_register_image')
    image_path = args.image_file.resolve(strict=True)
    arguments = {
        'filename': image_path.name,
        'image_base64': base64.b64encode(image_path.read_bytes()).decode('ascii'),
    }
elif args.audio_file:
    if args.tool != 'video_register_audio':
        parser.error('--audio-file is only valid with video_register_audio')
    audio_path = args.audio_file.resolve(strict=True)
    arguments = {
        'filename': audio_path.name,
        'audio_base64': base64.b64encode(audio_path.read_bytes()).decode('ascii'),
    }
else:
    arguments = json.loads(args.arguments_file.read_text() if args.arguments_file else args.arguments)
request = urllib.request.Request(
    'http://127.0.0.1:18189/dgx-drama/mcp' if args.drama else 'http://192.0.2.37:18082/mcp',
    data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list' if args.list else 'tools/call',
                     'params': {} if args.list else {'name': args.tool, 'arguments': arguments}}).encode(),
    headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
             'MCP-Protocol-Version': '2024-11-05' if args.drama else '2025-11-25',
             **({'X-DGX-Drama-Action': 'explicit-user-action'} if args.drama else {})},
)
with urllib.request.urlopen(request, timeout=45) as response:
    result = json.load(response)
print(json.dumps(result, ensure_ascii=False, indent=2))

if 'error' in result or result.get('result', {}).get('isError'):
    raise SystemExit(1)
