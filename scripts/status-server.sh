#!/usr/bin/env bash
set -euo pipefail
systemctl --user --no-pager --full status qwen38-27b.service
curl --fail --silent --show-error http://127.0.0.1:8080/health
printf '\n'
