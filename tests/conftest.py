import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from fastmcp import Client
from fastmcp import FastMCP

from netbox_mcp import retry
from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.tools import register_tools

# Returned for any request no test registered; not 404, so it never triggers
# the client's fallback-endpoint logic by accident.
UNMOCKED_STATUS = 418


@dataclass
class Recorded:
    method: str
    path: str
    query: list[tuple[str, str]]
    headers: httpx2.Headers
    json: Any


class FakeNetBox:
    """Answers NetBox API calls with canned responses, via httpx2.MockTransport."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list[tuple[int, Any]]] = defaultdict(list)
        self.requests: list[Recorded] = []
        self.transport = httpx2.MockTransport(self.handle)

    def add(self, method: str, path: str, *, status: int = 200, payload: Any = None):
        """Queue a response. The last response for a route repeats forever.

        Args:
            method: HTTP method.
            path: API path relative to /api/ ("dcim/devices"), or an absolute
                path starting with "/" ("/graphql/").
        """
        if not path.startswith("/"):
            path = f"/api/{path.strip('/')}/"
        self.routes[(method, path)].append((status, payload))

    def get(self, path: str, **kwargs):
        self.add("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        self.add("POST", path, **kwargs)

    def sent(self, method: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.requests.append(
            Recorded(
                request.method,
                path,
                list(request.url.params.multi_items()),
                request.headers,
                json.loads(request.content) if request.content else None,
            )
        )
        queue = self.routes.get((request.method, path))
        if not queue:
            return httpx2.Response(UNMOCKED_STATUS, json={"detail": "not mocked"})
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        if status == 204:
            return httpx2.Response(204)
        return httpx2.Response(status, json=payload)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    NetBoxClient.reset_cache()
    monkeypatch.setattr(retry, "BASE_BACKOFF", 0)
    yield
    NetBoxClient.reset_cache()


@pytest.fixture
def config() -> NetBoxConfig:
    return NetBoxConfig(netbox_url="https://netbox.test", token="nbt_abc.secret")


@pytest.fixture
def netbox() -> FakeNetBox:
    return FakeNetBox()


def build_server(
    config: NetBoxConfig,
    transport: httpx2.AsyncBaseTransport,
    *,
    read_only: bool = False,
) -> FastMCP:
    mcp = FastMCP("test")
    register_tools(mcp, config, transport)
    if read_only:
        mcp.enable(tags={"read-only"}, only=True)
    return mcp


@pytest.fixture
def client_for(netbox):
    def factory(config: NetBoxConfig, **kwargs) -> Client:
        return Client(build_server(config, netbox.transport, **kwargs))

    return factory
