# Operator bundle TDD evidence

## RED — 2026-09-01 Asia/Shanghai

Command:

```bash
python3 -m unittest -v ComfyUI/tests/scripts/test_operator_bundle.py
```

Observed result before production files existed:

```text
Ran 8 tests in 0.002s
FAILED (failures=8)
```

Every failure was the intended missing-feature assertion:

- `bootstrap script missing` (three secret-bootstrap behaviors);
- `manage script missing` (lifecycle and read-only doctor behaviors);
- `video evidence builder missing` (hash-bound evidence and S-CUI-1 reservation);
- `workflow missing` (T2VA-only official graph).

The earlier harness draft raised `FileNotFoundError`; the tests were corrected
before implementation so the retained RED run is eight behavior failures, not
test-construction errors.

## GREEN

Initial implementation exposed two integration defects while the suite stayed
red: macOS Bash 3.2 has no `mapfile`, and the installed ffmpeg has no
`drawtext`. The implementation was corrected to use a portable read loop and
Pillow-based visibly labeled contact sheets. The resulting eight-test run was:

```text
Ran 8 tests in 2.520s
OK
```

A second RED cycle added three omitted contract assertions: `start` must run
the read-only H3/DeepSeek doctor before Compose, semantic review must bind
scenario/media/sample hashes, and S-CUI-1 must bind a user-confirmation
artifact. The focused run failed all three for the intended missing behavior:

```text
Ran 3 tests in 1.930s
FAILED (failures=3)
```

After the minimal implementation, the final full run was:

```text
Ran 9 tests in 3.116s
OK
```

A third RED test then demonstrated that a silent video returned exit 0 even
though its machine gate was false:

```text
Ran 1 test in 0.706s
FAILED (failures=1)
```

The builder now returns nonzero, writes `FAIL_MEDIA`, and retains the hash-bound
failure package for diagnosis. Final suite:

```text
Ran 10 tests in 5.192s
OK
```

No real SSH, token read, Docker lifecycle, H3 request, or DeepSeek operation was
performed by these tests; fake executables recorded the exact command surface.
