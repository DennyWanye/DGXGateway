#!/usr/bin/env python3
"""Temporary loopback CONNECT proxy for an isolated image build, UID 996 only."""
import ipaddress
import os
import select
import socket
import socketserver
import time

ALLOWED = {'ports.ubuntu.com', 'pypi.org', 'files.pythonhosted.org'}


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        try:
            self.request.settimeout(15)
            data = b''
            while b'\r\n\r\n' not in data:
                block = self.request.recv(4096)
                if not block:
                    return
                data += block
                if len(data) > 16384:
                    return
            headers, remainder = data.split(b'\r\n\r\n', 1)
            method, authority, protocol = headers.split(b'\r\n', 1)[0].decode('ascii').split()
            host, port = authority.rsplit(':', 1)
            if method != 'CONNECT' or protocol not in ('HTTP/1.0', 'HTTP/1.1') or host not in ALLOWED or port != '443':
                self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                return
            addresses = socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
            for address in addresses:
                ip = address[4][0]
                if not ipaddress.ip_address(ip).is_global:
                    continue
                try:
                    upstream = socket.create_connection((ip, 443), timeout=15)
                    break
                except OSError:
                    pass
            if upstream is None:
                raise OSError('No reachable public address')
            self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            if remainder:
                upstream.sendall(remainder)
            # TLS stays end-to-end between apt/pip and the allowlisted publisher.
            end = time.monotonic() + 1800
            while time.monotonic() < end:
                ready, _, _ = select.select([self.request, upstream], [], [], 60)
                if not ready:
                    break
                for source in ready:
                    block = source.recv(65536)
                    if not block:
                        return
                    (upstream if source is self.request else self.request).sendall(block)
        except (OSError, ValueError, UnicodeError):
            # Never log request headers, payloads, or URL values.
            pass
        finally:
            if upstream is not None:
                upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    if os.getuid() != 996 or socket.gethostname() != 'dgx-head':
        raise SystemExit('Expected head direct-download identity UID 996')
    with Server(('127.0.0.1', 18763), Handler) as server:
        print('LongCat build proxy ready on loopback; three publisher domains only', flush=True)
        server.serve_forever()
