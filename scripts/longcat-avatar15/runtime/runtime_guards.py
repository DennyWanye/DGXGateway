"""Pure Python guards for the pinned LongCat Avatar 1.5 clean-speech entry."""
from pathlib import Path
import hashlib

DMD_SIZE = 2523077984
DMD_SHA256 = "d969115a7f3fbc212a277ed4399f77a1a065d170148c6d4bc272e980cd3e907a"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path, expected_size, expected_sha256):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Required artifact missing: {path}")
    if path.stat().st_size != expected_size:
        raise ValueError(f"Artifact size mismatch: {path}")
    if sha256_file(path) != expected_sha256:
        raise ValueError(f"Artifact SHA-256 mismatch: {path}")
    return str(path)


def require_dmd(checkpoint_dir):
    return verify_file(Path(checkpoint_dir) / "lora/dmd_lora.safetensors", DMD_SIZE, DMD_SHA256)


def require_clean_audio(path):
    # Read in place. This path is never passed to a delete operation.
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Clean audio must be an existing regular file")
    return str(path), sha256_file(path)


def verify_audio_unchanged(path, original_sha256):
    if sha256_file(path) != original_sha256:
        raise RuntimeError("Original audio changed during inference")


def load_required_dmd(dit, path):
    dit.load_lora(path, "dmd", multiplier=1.0, lora_network_dim=128, lora_network_alpha=64)
    if "dmd" not in dit.lora_dict or not dit.lora_dict["dmd"].loras:
        raise RuntimeError("DMD load returned without any registered adapters")
    dit.enable_loras(["dmd"])
    if "dmd" not in dit.active_loras:
        raise RuntimeError("DMD activation failed")
