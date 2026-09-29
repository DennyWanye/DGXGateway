from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


ACTIVE_STATES = frozenset({"STARTING", "HEALTHY"})
AMBIGUOUS_STATES = frozenset({"UNKNOWN", "DEGRADED", "PARTIAL"})


@dataclass(frozen=True)
class ObservedModel:
    model_id: str
    state: str
    profile: str | None = None


@dataclass(frozen=True)
class ActiveModelClassification:
    state: str
    model_id: str | None = None
    profile: str | None = None


def classify_local_models(models: Iterable[ObservedModel]) -> ActiveModelClassification:
    observed = tuple(models)
    active = tuple(item for item in observed if item.state in ACTIVE_STATES)
    if len(active) > 1:
        return ActiveModelClassification("CONFLICT")
    if any(item.state in AMBIGUOUS_STATES for item in observed):
        return ActiveModelClassification("AMBIGUOUS")
    if len(active) == 1:
        item = active[0]
        return ActiveModelClassification("ACTIVE", item.model_id, item.profile)
    return ActiveModelClassification("NONE")
