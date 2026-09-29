from __future__ import annotations

import argparse
from pathlib import Path

from .adapters.catalog import build_adapters
from .authority import GatewayLease
from .config import load_config
from .operations import OperationController
from .server import GatewayApplication, serve
from .snapshot import SnapshotService
from .store import OperationStore
from .providers import default_providers
from .admission import MutationAdmission


def build_application(config_path: Path, *, recover_incomplete: bool = True) -> GatewayApplication:
    config = load_config(config_path)
    store = OperationStore(config.state_dir / "gateway.sqlite3")
    incomplete = store.active_operation()
    if recover_incomplete and incomplete is not None:
        store.set_phase(
            incomplete.operation_id,
            "RECOVERY_REQUIRED",
            error="Gateway restarted during a non-terminal operation; manual recovery proof required",
        )
        if incomplete.outcome is None:
            store.freeze_outcome(incomplete.operation_id, "RECOVERY_REQUIRED")
    adapters = build_adapters(config)
    catalog_adapters = adapters
    operations = OperationController(
        store=store,
        adapters=adapters,
        lease_factory=lambda operation_id: GatewayLease(
            lock_cli=config.lock_cli,
            lock_root=config.lock_root,
            token_dir=config.token_dir,
            operation_id=operation_id,
            purpose=f"exclusive local model switch {operation_id}",
        ),
        acceptance_failure_model=config.acceptance_failure_model,
    )
    app = GatewayApplication(
        config=config,
        store=store,
        operations=operations,
        snapshot=SnapshotService(config, store, catalog_adapters, providers=default_providers()),
        static_dir=Path(__file__).resolve().parents[1] / "static",
        admission=MutationAdmission(config),
    )

    from .creative_jobs import CreativeJobs
    app.creative_jobs = CreativeJobs(app)
    from .video_jobs import VideoJobs
    app.video_jobs = VideoJobs(app)
    from .avatar_jobs import AvatarJobs
    app.avatar_jobs = AvatarJobs(app)
    from .text_jobs import TextJobs
    app.text_jobs = TextJobs(app, recover_on_startup=recover_incomplete)
    from .text_qualification import apply_qualification
    apply_qualification(app.text_jobs)
    from .media_jobs import MediaJobs
    app.media_jobs = MediaJobs(app)
    from .residency import ModelResidency
    app.residency = ModelResidency(app)
    from .recording import load_recording
    app.recording = load_recording(config_path, config, app.snapshot)
    if config.drama_enabled:
        from .drama.service import DramaService
        from .drama.gateway_adapter import GatewayReferenceAdapter
        from .drama.gateway_video_adapter import GatewayVideoAdapter
        from .drama.gateway_text_adapter import GatewayTextAdapter
        app.drama = DramaService(config.state_dir, config.repo_root, GatewayReferenceAdapter(app=app), GatewayVideoAdapter(app=app), GatewayTextAdapter(app=app))
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="DGX Cluster Gateway")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    serve(build_application(args.config))


if __name__ == "__main__":
    main()
