from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapability:
    provider_id: str
    state: str
    tasks: tuple[str, ...]
    reason: str | None = None

    def to_dict(self) -> dict:
        return {"provider_id": self.provider_id, "state": self.state, "tasks": list(self.tasks), "reason": self.reason}


class UnconfiguredProvider:
    def __init__(self, provider_id: str, tasks: tuple[str, ...]):
        self.provider_id = provider_id
        self.tasks = tasks

    def capability(self) -> ProviderCapability:
        return ProviderCapability(self.provider_id, "UNCONFIGURED", self.tasks, "credentials_not_configured")

    def submit(self, request: dict) -> str:
        del request
        raise RuntimeError("provider is unconfigured")

    def status(self, job_id: str) -> dict:
        del job_id
        raise RuntimeError("provider is unconfigured")

    def cancel(self, job_id: str) -> None:
        del job_id
        raise RuntimeError("provider is unconfigured")


class FakeProvider:
    """In-memory contract oracle that never performs network or billable work."""

    provider_id = "fake-video"

    def __init__(self):
        self.jobs: dict[str, str] = {}

    def capability(self) -> ProviderCapability:
        return ProviderCapability(self.provider_id, "CONFIGURED_FAKE", ("text-to-video", "image-to-video"))

    def submit(self, request: dict) -> str:
        if set(request) - {"task", "prompt", "image_ref"}:
            raise ValueError("unsupported request field")
        if request.get("task") not in self.capability().tasks:
            raise ValueError("unsupported task")
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = "QUEUED"
        return job_id

    def status(self, job_id: str) -> dict:
        return {"job_id": job_id, "state": self.jobs[job_id]}

    def cancel(self, job_id: str) -> None:
        if self.jobs[job_id] in {"QUEUED", "RUNNING"}:
            self.jobs[job_id] = "CANCELED"


def default_providers() -> tuple[UnconfiguredProvider, ...]:
    return (
        UnconfiguredProvider("seedance", ("text-to-video", "image-to-video", "video-to-video")),
        UnconfiguredProvider("veo", ("text-to-video", "image-to-video")),
    )
