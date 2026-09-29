# Container packaging TDD evidence

This file records the required red/green cycle for the container and secret boundary.

## RED

Command: `python3 -m unittest discover -s ComfyUI/tests/container -p 'test_*.py' -v`

Observed before implementation: 11 tests ran and all 11 failed because the
required Dockerfile, Compose files, contracts, locks and entrypoint scripts did
not exist. The runtime tests failed at the explicit missing-entrypoint
assertion. This is the expected behavior under test, not a syntax/import error.

## GREEN

Command: `python3 -m unittest discover -s ComfyUI/tests/container -p 'test_*.py' -v`

Result after implementation: `Ran 11 tests in 1.179s` and `OK`.

The green run includes three real Docker executions against the pinned Python
base: valid mode-0600 secret and zero-capability UID/GID 65532 transition,
mode-0644 rejection, and production endpoint-environment rejection. The source
secret is nested beneath a root-owned mode-0700 tmpfs, which preserves the
post-drop unreadability assertion under Docker Desktop file-sharing semantics.

Additional green checks:

- `docker compose -f ComfyUI/compose.yaml config --quiet`
- `docker compose -f ComfyUI/compose.test.yaml config --quiet`
- shell syntax checks for entrypoint, steady-state, lock compiler and runner
- `git diff --check -- ComfyUI`
- pinned official source-stage build for `linux/arm64`, manifest
  `sha256:07a94f86d1994d67d0ad31d76041a90054b20e7f49fa46ccf5c98089417da966`

The full dependency-stage download was intentionally stopped after the green
checkpoint so the integration owner can perform the single authoritative full
image build instead of duplicating a large workflow-template download.
