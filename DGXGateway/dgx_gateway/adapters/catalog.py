"""Live Gateway catalog: retired two-node models are gone; remaining identities stay."""
from .h3 import H3Adapter
from .h3_ring import H3RingAdapter
from .h3_dual_pair import H3DualPairAdapter
from .longcat import LongCatAdapter
from .mia_dsv41 import MiaDsv41Adapter
from .music3 import ComfyAdapter
from .personaplex import PersonaPlexAdapter
from .dsv41_ring import Dsv41RingAdapter

RETIRED_MODEL_IDS = (
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "qwen38-flash-next",
)

CATALOG = (
    MiaDsv41Adapter,
    LongCatAdapter,
    H3Adapter,
    H3RingAdapter,
    H3DualPairAdapter,
    ComfyAdapter,
    PersonaPlexAdapter,
    Dsv41RingAdapter,
)


def build_adapters(config):
    return {cls.model_id: cls(config) for cls in CATALOG}
