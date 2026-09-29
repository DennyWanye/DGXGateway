# Third-party sources and licensing

This repository publishes a sanitized deployment/control-plane snapshot. It does not distribute model weights or grant additional rights to upstream models. Model access conditions and licenses remain with their respective repositories, referenced by the pinned download manifests.

- The workbench includes portions derived from Freecut. Preserve the license in `ComfyUI/custom_nodes/dgx_remote_h3/frontend/src/vendor/freecut/LICENSE` and notices in the frontend/public and built studio directories.
- The GGUF Comfy integration retains `scripts/comfy-gguf-pinned/LICENSE`.
- H3 overlays contain or modify SGLang code pinned at `e970453b433e1467e22f6d925f270563417540b5`. Upstream: https://github.com/sgl-project/sglang . The pinned license is included in `LICENSES/SGLang-LICENSE`. Follow the upstream license; these files are not independently relicensed.
- The DeepSeek Ring package references its recipe and ring upstreams, whose LICENSE/THIRD_PARTY_NOTICES files are preserved under `LICENSES/DeepSeek-*`. The upstream sources themselves are fetched by the package script.
- The LLaDA third-party integration implementation is not redistributed because no explicit integration-code license was found; its README points to the upstream installation source. LongCat runtime patches retain their upstream source references and obligations.

No blanket license has been added on the user's behalf for original project code. Public visibility alone is not an additional open-source license. Existing third-party licenses are preserved and are not superseded by this notice.
