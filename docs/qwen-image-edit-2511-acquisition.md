# Qwen Image Edit 2511 acquisition

2026-09-05. Official Comfy-Org artifact inventory verified through Hugging Face model API (`blobs=true`) before downloading. FP8 mixed diffusion model is present in the official repository. No additional quantization package is installed.

Sources: [Comfy native tutorial](https://docs.comfy.org/tutorials/image/qwen/qwen-image-edit-2511), [official edit repository](https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI), [official encoder/VAE repository](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI). The tutorial lists BF16 as its default; this acquisition explicitly chooses the official repo's FP8 mixed variant.

- `diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors`
  - Repository: `Comfy-Org/Qwen-Image-Edit_ComfyUI`
  - Revision: `984166f60a9b1fcede5e9b9287b7a7aebc050010`
  - Bytes: `20533762817`
  - SHA-256: `c9fdc158e46d3b61ef75f21ae866ca2fe808bf4a53643120d1c1e87c19280a4e`
- `text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors`
  - Repository: `Comfy-Org/Qwen-Image_ComfyUI`
  - Revision: `7beb7b647f04469fbe64ba8adc2bb0d7e5e9f73f`
  - Bytes: `9384670680`
  - SHA-256: `cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4`
- `vae/qwen_image_vae.safetensors`
  - Repository: `Comfy-Org/Qwen-Image_ComfyUI`
  - Revision: `7beb7b647f04469fbe64ba8adc2bb0d7e5e9f73f`
  - Bytes: `253806246`
  - SHA-256: `a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f`

Destination root: `/home/operator/ai/ComfyUI/models`. Manifest: `manifests/qwen-image-edit-2511-download.json`. Controller: existing `scripts/run-creative-image-download-queue.sh`; unique local log/PID `logs/qwen-image-edit-2511-download.{log,pid}`. Large payloads use UID 996 `dgx-hf-direct` via `wlP9s9`; operator remains on `dgx-vpn`. Partial files stage under `/var/lib/dgx-video-download`, then verified size/SHA files are promoted to final paths. Control metadata uses the established checksum-verified mirror path while payloads use official signed CDN URLs. Never print URL control files or raw remote downloader logs.

Acquisition does not start any model or change Gateway lifecycle. Actual native editing inference and memory release must be validated separately through Gateway.

## Completed verification

All three final files were checksum-promoted by the established controller and independently read/rehashed on head on 2026-09-05 after download. Exact byte counts and SHA-256 matched the pinned official API metadata. Machine-readable proof is `manifests/qwen-image-edit-2511-verification.json`, with remote copy `/home/operator/dgx-models/creative-model-download-logs/qwen-image-edit-2511-verification.json`. No download stalls or restarts occurred in this acquisition. Diffusion safetensors header parsed successfully (4,451 tensors; F32/BF16/F8_E4M3/U8 and `_quantization_metadata`). No model was loaded by these checks.
