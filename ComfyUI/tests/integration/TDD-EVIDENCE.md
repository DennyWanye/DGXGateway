# Integration TDD evidence

## RED

`python -m unittest ComfyUI/tests/integration/test_fake_gateway.py -v`
initially ran four tests and failed all four with `FileNotFoundError` for
`ComfyUI/tests/fake_gateway.py`. This proved that the Compose-declared test
service had no executable implementation.

After the fake gateway was implemented, a packaging test was added for the
test-only mode selector. It failed because `compose.test.yaml` did not contain
`${FAKE_GATEWAY_MODE:-success}`.

The integration review also added a short-write transport regression test. It
failed with `b'012' != b'0123456789abcdef'`, proving that one `os.write()` call
could silently truncate a downloaded MP4. The immutable test-profile selector
tests failed because `EXPECTED_PROFILE` and `contracts/test-deployment.py` did
not yet exist.

## GREEN

- Fake gateway unit contract: 4/4 PASS.
- Short-write and immutable-profile targeted suites: 19/19 PASS.
- Full custom-node suite using the disposable PyAV venv: 28/28 PASS.
- Operator/workflow suite: 10/10 PASS.
- Static packaging suite: 9/9 PASS before the Docker runtime cases.

The fake gateway is reachable only on the Compose `internal: true` network,
uses a canary-only token, validates the exact eight-field H3 policy, exposes a
request counter, and implements deterministic success/auth/busy/non-MP4/
redirect/empty/oversize/truncated modes. The production image contains neither
the fake endpoint nor a profile/endpoint environment override.
