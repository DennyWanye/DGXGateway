"""Bounded-connect synchronous H3 transport, with no long-profile response deadline."""
import http.client
import json
import threading
import urllib.error
from urllib.parse import urlsplit

from .job_cancel import JobCanceled


class LongVideoTransport:
    connect_timeout = 10
    cancel_timeout = 5
    cancel_poll_seconds = 0.5

    def read(self, endpoint, request, maximum, canceled, on_cancel_receipt):
        url = urlsplit(endpoint)
        if url.scheme != "http" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("H3 endpoint must be a fixed direct HTTP endpoint")
        connection = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=self.connect_timeout)
        finished = threading.Event()
        watcher = None
        headers = dict(request.header_items())
        request_id = request.get_header("X-h3-request-id")
        authorization = request.get_header("Authorization")
        def watch():
            while not finished.wait(self.cancel_poll_seconds):
                if not canceled():
                    continue
                cancel = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=self.cancel_timeout)
                try:
                    cancel.request("POST", "/v1/videos/cancel", body=b"", headers={"Authorization": authorization, "X-H3-Request-ID": request_id})
                    response = cancel.getresponse()
                    # An acknowledgement never closes the active generation socket.
                    status = response.status
                    response.read(8192)
                    on_cancel_receipt(status)
                    if status == 200:
                        return
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    cancel.close()
        try:
            # Connect/send remain bounded. Waiting for headers and response bytes
            # is unlimited only after a real connection is established.
            connection.request("POST", url.path, body=request.data, headers=headers)
            connection.sock.settimeout(None)
            watcher = threading.Thread(target=watch, daemon=True, name="h3-persistent-cancel")
            watcher.start()
            response = connection.getresponse()
            if response.status != 200:
                payload = response.read(65537)
                if (response.status == 500 and canceled() and len(payload) <= 65536
                        and b"H3 request canceled at a collective safe boundary" in payload):
                    # This exact exception is emitted only after the verified
                    # two-rank executor has joined its generation replies.
                    raise JobCanceled("H3 confirmed cancellation at a collective safe boundary")
                raise urllib.error.HTTPError(endpoint, response.status, "H3 response rejected", response.headers, None)
            content_type = response.headers.get_content_type()
            return content_type, response.read(maximum + 1)
        finally:
            finished.set()
            if watcher is not None:
                watcher.join(timeout=self.cancel_timeout + 1)
            connection.close()
