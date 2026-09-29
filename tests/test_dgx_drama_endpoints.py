"""Deployment endpoint separation, without ComfyUI/network/model dependencies."""
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).parents[1]


def load(override=None, gateway="http://192.0.2.37:18082/mcp"):
    name = "drama_endpoint_fixture_" + uuid.uuid4().hex
    package = ModuleType(name)
    package.__path__ = []
    client = ModuleType(name + ".creative_client")
    client.GATEWAY = gateway
    aiohttp = ModuleType("aiohttp")
    aiohttp.web = SimpleNamespace()
    server = ModuleType("server")
    routes = SimpleNamespace(get=lambda _: lambda fn: fn, post=lambda _: lambda fn: fn)
    server.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=routes))
    spec = importlib.util.spec_from_file_location(name + ".drama_routes", ROOT / "ComfyUI/custom_nodes/dgx_remote_h3/drama_routes.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: package, client.__name__: client, "aiohttp": aiohttp, "server": server}), patch.dict(os.environ):
        os.environ.pop("DGX_DRAMA_GATEWAY_URL", None)
        if override is not None:
            os.environ["DGX_DRAMA_GATEWAY_URL"] = override
        spec.loader.exec_module(module)
    return module, client


class DramaEndpointTests(unittest.TestCase):
    def test_missing_override_preserves_existing_gateway_origin(self):
        for gateway in ("http://192.0.2.37:18082/mcp", "http://host.docker.internal:18084/mcp"):
            with self.subTest(gateway=gateway):
                module, client = load(gateway=gateway)
                self.assertEqual(module.UPSTREAM, gateway.removesuffix("/mcp") + "/api/v1/drama")
                self.assertEqual(client.GATEWAY, gateway)

    def test_explicit_business_origin_does_not_change_creative_gateway(self):
        module, client = load("http://host.docker.internal:18084/api/v1/drama")
        self.assertEqual(module.UPSTREAM, "http://host.docker.internal:18084/api/v1/drama")
        self.assertEqual(client.GATEWAY, "http://192.0.2.37:18082/mcp")
        self.assertEqual(module.GATEWAY, client.GATEWAY)
        with patch.dict(os.environ, {"DGX_DRAMA_GATEWAY_URL": "http://127.0.0.1:9999/api/v1/drama"}):
            self.assertEqual(module.UPSTREAM, "http://host.docker.internal:18084/api/v1/drama")

    def test_explicit_trusted_lan_and_loopback_endpoints_are_allowed(self):
        for host in ("localhost", "host.docker.internal", "127.0.0.1", "[::1]", "192.0.2.37", "192.0.2.3", "192.0.2.25"):
            for scheme in ("http", "https"):
                with self.subTest(host=host, scheme=scheme):
                    url = f"{scheme}://{host}:18084/api/v1/drama"
                    self.assertEqual(load(url)[0].UPSTREAM, url)

    def test_invalid_override_is_rejected_without_echoing_value_or_fallback(self):
        for value in (
            "", "http://8.8.8.8:18084/api/v1/drama", "http://169.254.169.254:80/api/v1/drama",
            "http://untrusted.example:18084/api/v1/drama", "http://172.32.0.1:18084/api/v1/drama",
            "http://user:private-sentinel@127.0.0.1:18084/api/v1/drama",
            "http://127.0.0.1:18084/api/v1/drama?token=private-sentinel",
            "http://127.0.0.1:18084/api/v1/drama#private-sentinel", "http://127.0.0.1:18084/mcp",
            "http://127.0.0.1:18084/api/v1/drama/", "http://127.0.0.1/api/v1/drama",
            "http://127.0.0.1:0/api/v1/drama", "http://127.0.0.1:65536/api/v1/drama",
            "ftp://127.0.0.1:18084/api/v1/drama", " http://127.0.0.1:18084/api/v1/drama",
            "http://host.\ndocker.internal:18084/api/v1/drama",
            "\x00http://127.0.0.1:18084/api/v1/drama",
            "\x01http://127.0.0.1:18084/api/v1/drama",
            "\x1fhttp://127.0.0.1:18084/api/v1/drama",
            "http://127.0.0.1:18084/api/v1/drama\x7f",
        ):
            with self.subTest(case=value.split(":", 1)[0]), self.assertRaises(ValueError) as caught:
                if "\x00" in value:
                    # The OS itself rejects NUL in environ; exercise the same
                    # validator directly for this raw parser edge case.
                    load()[0].drama_upstream("http://192.0.2.37:18082/mcp", value)
                else:
                    load(value)
            self.assertEqual(str(caught.exception), "DGX_DRAMA_GATEWAY_URL must be a fixed trusted LAN/loopback drama endpoint")
            self.assertNotIn("private-sentinel", str(caught.exception))
