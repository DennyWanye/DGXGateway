# Ref2VA optional inputs overlay

This overlay implements the user's 2026-09-26 contract: text, images, video,
and audio are independently optional, and zero references still run Ref2VA.
It applies only to SGLang commit
`e970453b433e1467e22f6d925f270563417540b5`.

`scripts/profile_node.py` validates `manifest.json`, hashes each patched file,
and adds read-only mounts for Ref2VA profiles. The hashes are part of the
runtime fingerprint. FL2VA receives no overlay.

The three changes are deliberately separate:

- `request_validation.py` accepts empty reference conditions and an empty
  string prompt for Ref2VA; other task validation is unchanged.
- `presentation.py` keeps the supplied text and reference labels. When both
  are absent, the tokenizer's own EOS token supplies a nonzero empty-context
  sequence. No scene description or reference asset is fabricated.
- `stages/denoising.py` recognizes Ref2VA from the resolved task even when all
  reference rows are absent, preserving the Ref2VA packing path.

`verify_optional_inputs.py` runs CPU-only against installed SGLang. It checks
all 16 combinations of text/image/video/audio presence, empty-context packing,
and unchanged validation for other tasks. Its tokenizer and media are test
fixtures; it is not GPU inference or a quality assessment.

Deployment and actual inference evidence are recorded in
the workbench checkpoint（历史内部记录，公开版不包含）.
Model stop/start must go through Gateway. Never replace files in a running
model process and assume the imported modules changed.
