from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import signal
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from .operation_view import operation_view
from .comfy_identity import canonical_service


class GatewayApplication:
    def __init__(self, *, config, store, operations, snapshot, static_dir: Path, admission):
        self.config = config
        self.store = store
        self.operations = operations
        self.snapshot = snapshot
        self.static_dir = static_dir.resolve()
        self.admission = admission
        self.sessions: dict[str, float] = {}
        self._submit_lock = threading.RLock()

    def admin_token(self) -> str:
        path = self.config.admin_token_file
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise RuntimeError("admin token file is missing or unsafe")
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 32:
            raise RuntimeError("admin token is invalid")
        return value

    def create_session(self, supplied: str) -> str | None:
        if not hmac.compare_digest(supplied, self.admin_token()):
            return None
        session = secrets.token_urlsafe(32)
        self.sessions[session] = __import__("time").time() + 8 * 3600
        return session

    def authorized(self, cookie: str | None) -> bool:
        if getattr(self.config, "lan_no_auth", False):
            return True
        if not cookie:
            return False
        items = {}
        for part in cookie.split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                items[key] = value
        session = items.get("dgx_gateway_session")
        expiry = self.sessions.get(session or "", 0)
        return bool(session and expiry > __import__("time").time())

    def recover_stop(self, operation_id: str):
        """Explicit safe-stop recovery, preserving original failed outcome."""
        from .authority import GatewayLease
        from .operations import OperationContext
        with self._submit_lock:
            self.admission.assert_allowed()
            text_jobs = getattr(self, "text_jobs", None)
            if text_jobs: text_jobs.assert_available()
            operation = self.store.get_operation(operation_id)
            if operation.phase != "RECOVERY_REQUIRED" or self.store.active_operation():
                raise ValueError("operation is not awaiting idle recovery")
            lease = self.operations.recovery_leases.get(operation_id)
            if lease is None:
                lease = GatewayLease(lock_cli=self.config.lock_cli, lock_root=self.config.lock_root,
                                     token_dir=self.config.token_dir, operation_id=operation_id,
                                     purpose="explicit safe-stop recovery")
            lease.guard()
            context = OperationContext(operation_id, lease.token_path, lease.lease_generation, lease.check_supervision)
            try:
                if operation.outcome == "NO_RESOURCES_ACQUIRED":
                    self.operations._release(operation_id, lease)
                    self.operations.recovery_leases.pop(operation_id, None)
                    return self.store.get_operation(operation_id)
                self.operations._guard_video_stop(operation, context)
                for model_id in dict.fromkeys([operation.target_model, operation.source_model]):
                    if model_id:
                        adapter = self.operations.adapter_for(model_id)
                        if adapter is None:
                            raise RuntimeError("recovery adapter is unavailable")
                        if model_id == "minimax-h3":
                            adapter.drain(context)
                        adapter.retire(context)
                        self.store.append_event(operation_id, "recovery-stopped-proof", adapter.stopped_proof(context))
                self.operations._prove_others_stopped(operation_id, context, None, "recovery-all-stopped-proof")
                self.store.append_event(operation_id, "recovery-safe-stop-complete", {"stopped": True})
                self.operations._release(operation_id, lease)
                self.operations.recovery_leases.pop(operation_id, None)
                return self.store.get_operation(operation_id)
            except BaseException as exc:
                self.operations._recovery_required(operation_id, lease, exc)
                raise

    def cancel_operation(self, operation_id: str):
        with self._submit_lock:
            self.admission.assert_allowed()
            text_jobs = getattr(self, "text_jobs", None)
            if text_jobs and text_jobs.persistence.child_binding(operation_id):
                raise BlockingIOError("Use text_cancel for the owning parent text job")
            return self.store.request_cancel(operation_id)

    def _execute_operation(self, operation_id: str):
        """Persist failures before the controller's own guarded lifecycle begins."""
        try:
            return self.operations.execute(operation_id)
        except BaseException as exc:
            current = self.store.get_operation(operation_id)
            if current.phase not in {"RELEASED", "CANCELED"}:
                self.store.set_phase(operation_id, "RECOVERY_REQUIRED", error=str(exc))
                if current.outcome is None:
                    self.store.freeze_outcome(operation_id, "RECOVERY_REQUIRED")
            return self.store.get_operation(operation_id)

    def submit_switch(self, target_model: str | None, profile: str | None, key: str, *, job_owner=None, non_preemptive=False, origin="api"):
        target_model = canonical_service(target_model)
        adapter = self.operations.adapter_for(target_model)
        if profile is None:
            profile = getattr(adapter, "default_profile", None)
        profiles = getattr(adapter, "profiles", ())
        if (profiles and profile not in profiles) or (not profiles and profile is not None):
            raise ValueError("invalid profile")
        with self._submit_lock:
            self.admission.assert_allowed()
            existing = next(
                (item for item in self.store.list_operations() if item.idempotency_key == key),
                None,
            )
            if existing:
                if existing.target_model != target_model or existing.target_profile != profile:
                    raise ValueError("idempotency key reused with different target")
                return existing, False
            text_jobs = getattr(self, "text_jobs", None)
            if text_jobs: text_jobs.assert_available()
            for manager_name in ("creative_jobs", "video_jobs", "avatar_jobs"):
                jobs = getattr(self, manager_name, None)
                releasing_owned_job = job_owner is not None and getattr(
                    getattr(self, 'residency', None), 'releasing_owner', None
                ) == job_owner
                if jobs and jobs.active and jobs.active != job_owner and not releasing_owned_job:
                    raise BlockingIOError("creative generation owns DGX")
            if self.store.active_operation():
                raise BlockingIOError("another mutation is active")
            latest = self.store.list_operations(limit=1)
            if latest and latest[0].phase == "RECOVERY_REQUIRED":
                raise RuntimeError("latest operation requires recovery")
            observed = [adapter.observe() for adapter in self.operations.adapters.values()]
            residency = getattr(self, 'residency', None)
            retained_claim = bool(residency and residency.allow_claim(job_owner, target_model, profile))
            if retained_claim:
                residency._prove()
            if residency and residency.row['state'] != 'NONE' and not job_owner:
                raise BlockingIOError('Release the Gateway-owned retained model before a manual switch')
            if non_preemptive and target_model is not None and not retained_claim:
                if not observed:
                    raise BlockingIOError("DGX busy: non-preemptive generation requires every model to be stopped")
                for item in observed:
                    if item.state == "STOPPED":
                        continue
                    if item.model_id == target_model and item.state in {"STARTING", "HEALTHY"}:
                        continue
                    raise BlockingIOError("DGX busy: non-preemptive generation requires every model to be stopped")
            video_jobs = getattr(self, "video_jobs", None)
            if target_model is None and job_owner and video_jobs and (video_jobs.active == job_owner or (residency and residency.row.get('manager') == 'video_jobs' and residency.row.get('owner_job_id') == job_owner)):
                video_jobs.assert_stop_ownership(job_owner, observed)
            from .domain import classify_local_models
            active = classify_local_models(observed)
            failed_h3 = next((item for item in observed if item.model_id == "minimax-h3" and item.state == "PARTIAL"), None)
            recover_h3_stop = target_model is None and failed_h3 is not None and all(
                item.model_id == "minimax-h3" or item.state == "STOPPED" for item in observed
            )
            if active.state in {"CONFLICT", "AMBIGUOUS"} and not recover_h3_stop:
                raise RuntimeError("local model state is not safe for mutation")
            source_model = "minimax-h3" if recover_h3_stop else active.model_id if active.state == "ACTIVE" else None
            source_profile = failed_h3.profile if recover_h3_stop else active.profile if active.state == "ACTIVE" else None
            operation = self.store.create_operation(
                uuid.uuid4().hex, source_model, target_model, key, profile,
                source_profile=source_profile,
            )
            self.store.append_event(operation.operation_id, 'request-origin', {'kind': 'task' if job_owner else origin, 'job_id': job_owner})
            if retained_claim:
                self.store.append_event(operation.operation_id, 'resident-handoff', dict(residency.row['handoff']))
            requested_profile = profile
            if (
                active.state == "ACTIVE"
                and canonical_service(active.model_id) == target_model
                and (not getattr(self.operations.adapter_for(target_model), "profiles", ()) or active.profile == requested_profile)
            ):
                self.store.append_event(operation.operation_id, "already-at-target", {
                    "model_id": target_model,
                    "profile": requested_profile,
                })
                self.store.freeze_outcome(operation.operation_id, "COMMITTED")
                self.store.set_phase(operation.operation_id, "RELEASED")
                return self.store.get_operation(operation.operation_id), True
            thread = threading.Thread(
                target=self._execute_operation,
                args=(operation.operation_id,),
                name=f"gateway-operation-{operation.operation_id}",
                daemon=True,
            )
            thread.start()
            return operation, True


def public_events(events: list[dict]) -> list[dict]:
    safe = []
    for event in events:
        value = dict(event)
        payload = dict(value.get("payload", {}))
        if payload.get("error"):
            payload["error"] = "operation_failed"
        value["payload"] = payload
        safe.append(value)
    return safe


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, application: GatewayApplication):
        self.application = application
        super().__init__(address, GatewayHandler)


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Successful dashboard polling is not an audit event; avoid a write per refresh.
        if self.command == "GET" and self.path.split("?", 1)[0] in (
            "/api/v1/snapshot", "/api/v1/recording/status", "/api/v1/recording/recent"
        ) and len(args) > 1 and str(args[1]) == "200":
            return
        # Never include request bodies/cookies.
        print(f"gateway-http {self.client_address[0]} {self.command} {self.path} " + format % args)

    def _json(self, status: int, value: dict[str, Any], headers: dict[str, str] | None = None):
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, maximum: int = 4096) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("content type")
        length = int(self.headers.get("Content-Length", "-1"))
        if length < 2 or length > maximum:
            raise ValueError("body length")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("body root")
        return value

    def _origin_allowed(self) -> bool:
        return self.headers.get("Origin") in self.server.application.config.allowed_origins

    def _mutation_allowed(self) -> bool:
        if getattr(self.server.application.config, "lan_no_auth", False):
            return not self.headers.get("Origin") or self._origin_allowed()
        return self._origin_allowed() and self.server.application.authorized(
            self.headers.get("Cookie")
        )

    def do_GET(self):
        from .avatar_routes import handle as handle_avatar
        if handle_avatar(self):
            return
        from .text_routes import handle as handle_text
        if handle_text(self):
            return
        from .drama.routes import handle as handle_drama
        if handle_drama(self):
            return
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            self._json(405, {"error": "SSE stream not offered"}, {"Allow": "POST"})
            return
        if parsed.path in ("/api/v1/recording/status", "/api/v1/recording/recent", "/api/v1/recording/range"):
            recorder = getattr(self.server.application, "recording", None)
            if parsed.path.endswith("/status"):
                self._json(200, {"enabled": False} if recorder is None else recorder.status())
                return
            if parsed.path.endswith("/range"):
                if recorder is None:
                    self._json(200, {"samples": [], "range": {"start": None, "end": None, "bucket_ms": 1, "available_start": None, "available_end": None}})
                    return
                query = parse_qs(parsed.query)
                try:
                    start_ms = int(query.get("start", [""])[0])
                    end_ms = int(query.get("end", [""])[0])
                    limit = int(query.get("limit", ["1440"])[0])
                except (TypeError, ValueError):
                    self._json(422, {"error": "invalid_range"})
                    return
                self._json(200, recorder.range(start_ms, end_ms, limit))
                return
            self._json(200, {"enabled": False, "samples": []} if recorder is None else {"samples": recorder.history()})
            return
        if parsed.path == "/api/v1/snapshot":
            try:
                value = self.server.application.snapshot.build()
                application = self.server.application
                residency = getattr(application, 'residency', None)
                value['residency'] = residency.view() if residency else {'state': 'NONE'}
                from .runtime_view import RuntimeViewProvider
                value['runtime'] = RuntimeViewProvider(application)(value)
                recorder = getattr(self.server.application, "recording", None)
                value["recording"] = recorder.status() if recorder else {"enabled": False}
                value["session"] = {
                    "lan_no_auth": getattr(self.server.application.config, "lan_no_auth", False),
                    "authenticated": self.server.application.authorized(
                        self.headers.get("Cookie")
                    )
                }
                self._json(200, value)
            except Exception:
                self._json(503, {"error": "snapshot_unavailable"})
            return
        if parsed.path == "/api/v1/providers":
            self._json(200, {
                "providers": [
                    provider.capability().to_dict()
                    for provider in self.server.application.snapshot.providers
                ]
            })
            return
        if parsed.path.startswith("/api/v1/operations/"):
            operation_id = parsed.path.rsplit("/", 1)[-1]
            try:
                operation = self.server.application.store.get_operation(operation_id)
                self._json(200, {
                    "operation": operation_view(operation),
                    "events": public_events(self.server.application.store.events(operation_id)),
                })
            except KeyError:
                self._json(404, {"error": "not_found"})
            return
        if parsed.path.startswith("/api/v1/artifacts/"):
            from urllib.parse import unquote
            root = (self.server.application.config.state_dir / "creative-artifacts").resolve()
            target = (root / unquote(parsed.path.removeprefix("/api/v1/artifacts/"))).resolve()
            if root not in target.parents or not target.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            import shutil
            with target.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        target = (self.server.application.static_dir / relative).resolve()
        if self.server.application.static_dir not in target.parents or not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        from .avatar_routes import handle as handle_avatar
        if handle_avatar(self):
            return
        from .text_routes import handle as handle_text
        if handle_text(self):
            return
        from .drama.routes import handle as handle_drama
        if handle_drama(self):
            return
        parsed = urlparse(self.path)
        try:
            body = self._read_json(maximum=16 * 1024 * 1024 if parsed.path == "/mcp" else 4096)
        except Exception:
            self._json(422, {"error": "invalid_request"})
            return
        if parsed.path == "/mcp":
            if not self._mutation_allowed():
                self._json(403, {"error": "forbidden"})
                return
            from .mcp import dispatch
            response = dispatch(self.server.application, body)
            if response is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._json(200, response)
            return
        if parsed.path == "/api/v1/session":
            if not self._origin_allowed() or set(body) != {"token"} or not isinstance(body["token"], str):
                self._json(403, {"error": "forbidden"})
                return
            try:
                session = self.server.application.create_session(body["token"])
            except Exception:
                self._json(503, {"error": "auth_unavailable"})
                return
            if not session:
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"authenticated": True}, {
                "Set-Cookie": f"dgx_gateway_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"
            })
            return
        if not self._mutation_allowed():
            self._json(403, {"error": "forbidden"})
            return
        if parsed.path == "/api/v1/residency/release":
            owner = body.get('owner_job_id')
            if set(body) != {'owner_job_id'} or not isinstance(owner, str):
                self._json(422, {'error': 'invalid_request'})
                return
            residency = getattr(self.server.application, 'residency', None)
            try:
                value = residency.release(owner)
                self._json(200, {'residency': value})
            except (BlockingIOError, RuntimeError, ValueError) as exc:
                self._json(409, {'error': str(exc)})
            return
        if parsed.path == "/api/v1/switches":
            if set(body) - {"target_model", "profile", "idempotency_key"}:
                self._json(422, {"error": "invalid_request"})
                return
            target = canonical_service(body.get("target_model"))
            profile = body.get("profile")
            key = body.get("idempotency_key")
            if (target is not None and target not in self.server.application.operations.adapters) or not isinstance(key, str) or not 8 <= len(key) <= 128:
                self._json(422, {"error": "invalid_request"})
                return
            adapter = self.server.application.operations.adapter_for(target)
            if profile is None:
                profile = getattr(adapter, "default_profile", None)
            profiles = getattr(adapter, "profiles", ())
            if (profiles and profile not in profiles) or (not profiles and profile is not None):
                self._json(422, {"error": "invalid_profile"})
                return
            try:
                operation, created = self.server.application.submit_switch(target, profile, key, origin="manual")
                self._json(202 if created else 200, {"operation": operation_view(operation)})
            except BlockingIOError:
                self._json(409, {"error": "busy"})
            except Exception:
                self._json(409, {"error": "unsafe_state"})
            return
        if parsed.path.startswith("/api/v1/operations/") and parsed.path.endswith("/cancel"):
            operation_id = parsed.path.split("/")[-2]
            try:
                operation = self.server.application.cancel_operation(operation_id)
                self._json(202, {"operation": operation_view(operation)})
            except KeyError:
                self._json(404, {"error": "not_found"})
            except (ValueError, BlockingIOError):
                self._json(409, {"error": "not_cancelable"})
            return
        self._json(404, {"error": "not_found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/v1/session" or not self._origin_allowed():
            self._json(403, {"error": "forbidden"})
            return
        cookie = self.headers.get("Cookie")
        if cookie:
            for part in cookie.split(";"):
                if part.strip().startswith("dgx_gateway_session="):
                    session = part.strip().split("=", 1)[1]
                    self.server.application.sessions.pop(session, None)
        self._json(200, {"authenticated": False}, {
            "Set-Cookie": "dgx_gateway_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
        })


    def do_HEAD(self):
        from .drama.routes import handle as handle_drama
        if not handle_drama(self):
            self.send_error(404)


def serve(application: GatewayApplication) -> None:
    server = GatewayHTTPServer((application.config.bind, application.config.port), application)
    recorder = getattr(application, "recording", None)
    if recorder:
        recorder.start()
    residency = getattr(application, 'residency', None)
    if residency: residency.start()
    previous_signal = None
    if threading.current_thread() is threading.main_thread():
        previous_signal = signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        if recorder:
            recorder.stop()
        drama = getattr(application, "drama", None)
        if drama:
            drama.close()
        if residency: residency.close()
        server.server_close()
        if previous_signal is not None:
            signal.signal(signal.SIGTERM, previous_signal)
