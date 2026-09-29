"""Fixed Gateway transport shared by graph nodes and same-origin routes."""
import json
import os
import urllib.request

GATEWAY = os.environ.get("DGX_CREATIVE_GATEWAY_URL", "http://192.0.2.37:18082/mcp")


def forward(payload, timeout=60):
    request = urllib.request.Request(GATEWAY, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


