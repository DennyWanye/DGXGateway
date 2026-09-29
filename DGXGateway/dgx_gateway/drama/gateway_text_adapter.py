"""Fixed-origin Gateway text API; no model endpoint is accepted from a client."""
import json
import http.client
import socket
import threading
import time
import re
import urllib.parse

from .gateway_adapter import RPC_ORIGIN


class GatewayTextBusy(RuntimeError):
    """A complete, fixed-origin TEXT_BUSY response proves no job was accepted."""
    def __init__(self, evidence):
        super().__init__('Gateway明确未受理：等待其他模型任务释放资源')
        self.evidence = evidence


class GatewayTextAdapter:
    def __init__(self, *, app=None, timeout=30, connection_factory=http.client.HTTPConnection):
        self.app = app
        self.timeout, self.connection_factory = timeout, connection_factory

    def _request(self, path, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        target = urllib.parse.urlsplit(RPC_ORIGIN)
        if target.scheme != "http" or target.username or target.password:
            raise RuntimeError("Invalid fixed Gateway origin")
        deadline = time.monotonic() + self.timeout
        def remaining():
            result = deadline - time.monotonic()
            if result <= 0: raise TimeoutError("Gateway text deadline exceeded")
            return result
        connection = self.connection_factory(target.hostname, target.port, timeout=min(10, remaining()))
        timer = None
        try:
            connection.connect()
            sock = connection.sock
            def expire():
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            timer = threading.Timer(remaining(), expire)
            timer.daemon = True; timer.start()
            headers = {"Content-Type": "application/json"}
            if body is not None:
                # This deployment-owned origin is fixed, never copied from a
                # browser or model payload. The Gateway checks its allowlist.
                headers["Origin"] = RPC_ORIGIN
            connection.request("GET" if body is None else "POST", path, body=data, headers=headers)
            sock.settimeout(remaining())
            response = connection.getresponse()
            status = response.status
            if status not in (200, 202) and not (status == 409 and path == '/api/v1/text-jobs' and body is not None):
                raise RuntimeError("Gateway text receipt is not confirmed")
            chunks, count = [], 0
            while True:
                sock.settimeout(remaining())
                chunk = response.read1(65536)
                remaining()
                count += len(chunk)
                if count > 16 * 1024 * 1024: raise RuntimeError("Gateway text receipt exceeds bound")
                chunks.append(chunk)
                if not chunk or response.isclosed():
                    if response.length not in (None, 0): raise RuntimeError("Incomplete Gateway text receipt")
                    break
            from ..text_jobs import strict_json
            value = strict_json(b"".join(chunks).decode("utf-8"))
            if (path == '/api/v1/text-jobs' and body is not None and status == 409
                    and isinstance(value, dict) and isinstance(value.get('error'), dict)
                    and value['error'].get('code') == 'TEXT_BUSY'):
                raise GatewayTextBusy({'status': status, 'body': value})
            if status != 200 and not (body is not None and status == 202):
                raise RuntimeError("Gateway text receipt is not confirmed")
            if not isinstance(value, dict) or value.get("error"):
                raise RuntimeError("Gateway text receipt is not confirmed")
        finally:
            if timer is not None: timer.cancel()
            connection.close()
        return value

    @staticmethod
    def _id(jid):
        if not isinstance(jid, str) or not re.fullmatch(r"[a-f0-9]{32}", jid):
            raise ValueError("Invalid Gateway text identity")
        return jid

    def capabilities(self):
        return self.app.text_jobs.capabilities() if self.app else self._request("/api/v1/text-capabilities")

    def submit(self, kind, inputs, key):
        if self.app:
            from ..text_jobs import TextJobError
            try: return self.app.text_jobs.submit(kind, inputs, key)
            except TextJobError as exc:
                if exc.status == 409 and exc.code == 'TEXT_BUSY':
                    raise GatewayTextBusy({'status': 409, 'body': {'error': {'code': exc.code, 'message': str(exc)}}}) from exc
                raise
        return self._request("/api/v1/text-jobs", {"task_kind": kind, "inputs": inputs, "idempotency_key": key})["job"]

    def find(self, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ValueError("Invalid Gateway request key")
        return self.app.text_jobs.find(key) if self.app else self._request("/api/v1/text-jobs/find?" + urllib.parse.urlencode({"key": key}))["job"]

    def get(self, jid):
        jid = self._id(jid)
        return self.app.text_jobs.get(jid) if self.app else self._request("/api/v1/text-jobs/" + jid)["job"]

    def cancel(self, jid):
        jid = self._id(jid)
        return self.app.text_jobs.cancel(jid) if self.app else self._request("/api/v1/text-jobs/" + jid + "/cancel", {})["job"]
