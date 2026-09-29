#!/usr/bin/env python3
"""Validate tracked public text files without contacting GPUs or reading live state."""
import ast
import json
from pathlib import Path
import re
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
counts = {'files': 0, 'python': 0, 'json': 0, 'toml': 0, 'shell': 0}
errors = []
for rel in filter(None, files):
    path = ROOT / rel
    counts['files'] += 1
    if path.is_symlink():
        errors.append({'path': rel, 'error': 'symlink in public snapshot'})
        continue
    if path.suffix in {'.safetensors', '.gguf', '.mp4', '.wav', '.sqlite', '.db', '.key', '.pem', '.token'}:
        errors.append({'path': rel, 'error': 'private state, media, model or credential filename'})
    try:
        source = path.read_text()
        if path.suffix == '.py':
            ast.parse(source); counts['python'] += 1
        elif path.suffix == '.json':
            json.loads(source); counts['json'] += 1
        elif path.suffix == '.toml':
            tomllib.loads(source); counts['toml'] += 1
        elif path.suffix == '.sh':
            result = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True)
            if result.returncode: raise ValueError(result.stderr)
            counts['shell'] += 1
        if path.suffix == '.md':
            for label, url in re.findall(r'(?<!!)\[([^\]]+)\]\(([^\s)]+)\)', source):
                if not url.startswith(('http', '#', 'mailto:')) and not (path.parent / url.split('#')[0]).exists():
                    errors.append({'path': rel, 'error': 'missing local documentation target: ' + url})
    except (ValueError, SyntaxError, UnicodeError) as exc:
        errors.append({'path': rel, 'error': str(exc)})
print(json.dumps({'counts': counts, 'errors': errors}, indent=2))
raise SystemExit(bool(errors))
