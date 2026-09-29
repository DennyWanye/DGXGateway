# Custom-node core TDD evidence

Date: 2026-09-01

All commands ran from `/Users/operator/projects/DGXSpark-worktrees/comfyui-remote-h3`.
Media tests used the disposable interpreter
`/tmp/dgx-comfyui-h3-unit-venv/bin/python` with `av==15.1.0`; no project or
system Python environment was modified.

## RED checkpoints

Each production module was absent when its first behavioral test was run.

```text
$ python3 -m unittest discover -s ComfyUI/tests/unit -p 'test_policy.py' -v
ModuleNotFoundError: No module named 'dgx_remote_h3'
FAILED (errors=1)

$ python3 -m unittest discover -s ComfyUI/tests/unit -p 'test_secret.py' -v
ModuleNotFoundError: No module named 'dgx_remote_h3.secret'
FAILED (errors=1)

$ python3 -m unittest discover -s ComfyUI/tests/unit -p 'test_storage.py' -v
ModuleNotFoundError: No module named 'dgx_remote_h3.storage'
FAILED (errors=1)

$ python3 -m unittest discover -s ComfyUI/tests/unit -p 'test_contract.py' -v
ModuleNotFoundError: No module named 'dgx_remote_h3.contract'
FAILED (errors=1)

$ python3 -m unittest discover -s ComfyUI/tests/unit -p 'test_client.py' -v
ModuleNotFoundError: No module named 'dgx_remote_h3.client'
FAILED (errors=1)

$ /tmp/dgx-comfyui-h3-unit-venv/bin/python -m unittest discover -s ComfyUI/tests/unit -p 'test_media.py' -v
ModuleNotFoundError: No module named 'dgx_remote_h3.media'
FAILED (errors=1)

$ /tmp/dgx-comfyui-h3-unit-venv/bin/python -m unittest discover -s ComfyUI/tests/unit -p 'test_service.py' -v
ModuleNotFoundError: No module named 'dgx_remote_h3.service'
FAILED (errors=1)

$ /tmp/dgx-comfyui-h3-unit-venv/bin/python -m unittest discover -s ComfyUI/tests/unit -p 'test_node.py' -v
ModuleNotFoundError: No module named 'dgx_remote_h3.node'
FAILED (errors=1)
```

The first integrated discovery then caught an eager ComfyUI import in the
package initializer. This was the expected failure proving that pure modules
were not yet usable without ComfyUI:

```text
$ /tmp/dgx-comfyui-h3-unit-venv/bin/python -m unittest discover -s ComfyUI/tests/unit -p 'test_*.py' -v
ModuleNotFoundError: No module named 'folder_paths'
Ran 19 tests
FAILED (errors=3)
```

The first node result test also exposed macOS `/var` versus `/private/var`
canonicalization. The expectation was corrected to compare the actual resolved
path; no production path check was weakened.

## GREEN checkpoints

After minimal implementations, the individual suites passed with these exact
counts: policy 3, secret 3, storage 4, contract 4, client 4, media 2, service 2,
and node 4. The final integrated command and output were:

```text
$ /tmp/dgx-comfyui-h3-unit-venv/bin/python -m unittest discover -s ComfyUI/tests/unit -p 'test_*.py' -v
Ran 26 tests in 2.533s

OK
```

The package initializer now imports `node.py` only inside `comfy_entrypoint`,
so transport, contract, media, policy, secret, service, and storage remain
independently testable without ComfyUI imports.
