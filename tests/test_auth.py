import asyncio
import logging

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.utilities.tests import asgi_client
from fastmcp.utilities.tests import asgi_server

from netbox_mcp import auth
from netbox_mcp import netbox_client
from netbox_mcp.auth import NetBoxTokenVerifier
from netbox_mcp.auth import NetBoxUnavailableError
from netbox_mcp.auth import build_auth_provider
from netbox_mcp.auth import rate_limit_client_id
from netbox_mcp.models import NetBoxConfig
from netbox_mcp.models import TransportConfig
from netbox_mcp.netbox_client import MissingTokenError
from netbox_mcp.netbox_client import NetBoxAPIError
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.netbox_client import token_fingerprint
from netbox_mcp.object_types import plugin_object_types
from netbox_mcp.retry import parse_retry_after
from netbox_mcp.tools import register_tools

SERVICE_TOKEN = "nbt_service.secret"
USER_TOKEN = "nbt_alice.secret"
USER = {"id": 7, "username": "alice", "is_active": True}


@pytest.fixture
def passthrough_config() -> NetBoxConfig:
    return NetBoxConfig(
        netbox_url="https://netbox.test", token=SERVICE_TOKEN, token_passthrough=True
    )


def auth_headers(netbox) -> list[str | None]:
    return [r.headers.get("authorization") for r in netbox.requests]


# ------------------------------------------------------------- verifier


async def test_verifier_accepts_token_netbox_accepts(passthrough_config, netbox):
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    access_token = await verifier.verify_token(USER_TOKEN)

    assert access_token is not None
    assert access_token.token == USER_TOKEN
    assert access_token.client_id == "user:7"
    assert auth_headers(netbox) == [f"Bearer {USER_TOKEN}"]


async def test_verifier_sends_v1_token_with_token_scheme(passthrough_config, netbox):
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    assert await verifier.verify_token("0123456789abcdef") is not None
    assert auth_headers(netbox) == ["Token 0123456789abcdef"]


@pytest.mark.parametrize("status", [401, 403])
async def test_verifier_remembers_rejection_until_reject_ttl(
    passthrough_config, netbox, monkeypatch, status
):
    now = 1000.0
    monkeypatch.setattr(auth.time, "monotonic", lambda: now)
    netbox.get("authentication-check", status=status, payload={"detail": "no"})
    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=netbox.transport, reject_ttl=30
    )

    assert await verifier.verify_token(USER_TOKEN) is None
    assert await verifier.verify_token(USER_TOKEN) is None
    assert len(netbox.requests) == 1
    assert verifier._cache == {}

    # The token was created in NetBox meanwhile: accepted once the TTL ends.
    netbox.routes.clear()
    netbox.get("authentication-check", payload=USER)
    now += 31
    assert await verifier.verify_token(USER_TOKEN) is not None
    assert len(netbox.requests) == 2


async def test_verifier_rejection_cache_is_bounded(passthrough_config, netbox):
    netbox.get("authentication-check", status=401, payload={"detail": "no"})
    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=netbox.transport, max_entries=2
    )

    for token in ("nbt_a.x", "nbt_b.x", "nbt_c.x"):
        await verifier.verify_token(token)

    assert len(verifier._rejected) == 2


async def test_verifier_caps_concurrent_checks(passthrough_config, netbox):
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(
        passthrough_config,
        transport=netbox.transport,
        max_concurrent_checks=1,
        timeout=0.05,
    )

    # Every slot is busy: a new token waits, then gets a 503, not a NetBox call.
    async with verifier._checks:
        with pytest.raises(NetBoxUnavailableError, match="TimeoutError"):
            await verifier.verify_token(USER_TOKEN)
    assert netbox.requests == []

    assert await verifier.verify_token(USER_TOKEN) is not None


async def test_token_check_is_not_retried(passthrough_config):
    calls = 0

    def busy(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            429, json={"detail": "busy"}, headers={"Retry-After": "8"}
        )

    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=httpx2.MockTransport(busy)
    )

    with pytest.raises(NetBoxUnavailableError) as excinfo:
        await verifier.verify_token(USER_TOKEN)

    assert calls == 1
    assert excinfo.value.retry_after == 8


@pytest.mark.parametrize("token", ["", "   ", "nbt_zażółć.x", "nbt_a\x00.x"])
async def test_verifier_rejects_malformed_token_locally(
    passthrough_config, netbox, token
):
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    assert await verifier.verify_token(token) is None
    assert netbox.requests == []


@pytest.mark.parametrize("status", [400, 405, 407, 410, 429, 500, 503])
async def test_verifier_reports_netbox_errors_as_unavailable(
    passthrough_config, netbox, status
):
    netbox.get("authentication-check", status=status, payload={"detail": "down"})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    with pytest.raises(NetBoxUnavailableError, match=f"HTTP {status}"):
        await verifier.verify_token(USER_TOKEN)
    assert verifier._cache == {}


@pytest.mark.parametrize("payload", [None, "<html>login</html>", ["x"]])
async def test_verifier_does_not_accept_a_non_object_answer(
    passthrough_config, netbox, payload
):
    netbox.get("authentication-check", payload=payload)
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    with pytest.raises(NetBoxUnavailableError, match="unexpected response"):
        await verifier.verify_token(USER_TOKEN)
    assert verifier._cache == {}


async def test_verifier_reports_unreachable_netbox_as_unavailable(passthrough_config):
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=httpx2.MockTransport(refuse)
    )

    with pytest.raises(NetBoxUnavailableError, match="ConnectError"):
        await verifier.verify_token(USER_TOKEN)


async def test_verifier_checks_concurrent_requests_once(passthrough_config, netbox):
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    results = await asyncio.gather(
        *(verifier.verify_token(USER_TOKEN) for _ in range(10))
    )

    assert all(r is not None and r.client_id == "user:7" for r in results)
    assert len(netbox.requests) == 1


async def test_verifier_evicts_least_recently_used(passthrough_config, netbox):
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=netbox.transport, max_entries=2
    )

    await verifier.verify_token("nbt_a.x")
    await verifier.verify_token("nbt_b.x")
    await verifier.verify_token("nbt_a.x")  # cache hit: a is now newest
    await verifier.verify_token("nbt_c.x")  # evicts b, not a
    sent = len(netbox.requests)
    await verifier.verify_token("nbt_a.x")

    assert len(netbox.requests) == sent


async def background_checks(verifier: NetBoxTokenVerifier) -> None:
    await asyncio.gather(*verifier._pending.values(), return_exceptions=True)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    return now


async def test_verifier_caches_until_ttl(passthrough_config, netbox, clock):
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=netbox.transport, ttl=60
    )

    await verifier.verify_token(USER_TOKEN)
    await verifier.verify_token(USER_TOKEN)
    assert len(netbox.requests) == 1

    # Stale: accepted at once, re-checked in the background.
    clock[0] += 61
    assert await verifier.verify_token(USER_TOKEN) is not None
    assert len(netbox.requests) == 1
    await background_checks(verifier)
    assert len(netbox.requests) == 2

    # The re-check made the entry fresh again.
    await verifier.verify_token(USER_TOKEN)
    assert verifier._pending == {}
    assert len(netbox.requests) == 2


async def test_stale_token_does_not_wait_for_busy_checks(
    passthrough_config, netbox, clock
):
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=netbox.transport, max_concurrent_checks=1
    )
    await verifier.verify_token(USER_TOKEN)

    # A flood of unknown tokens holds every check slot.
    clock[0] += 61
    async with verifier._checks:
        access_token = await asyncio.wait_for(
            verifier.verify_token(USER_TOKEN), timeout=1
        )
        assert access_token is not None
    await background_checks(verifier)


async def test_revoked_token_is_dropped_by_background_check(
    passthrough_config, netbox, clock
):
    netbox.get("authentication-check", payload=USER)
    netbox.get("authentication-check", status=401, payload={"detail": "revoked"})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)
    await verifier.verify_token(USER_TOKEN)

    clock[0] += 61
    assert await verifier.verify_token(USER_TOKEN) is not None
    await background_checks(verifier)

    assert verifier._cache == {}
    assert await verifier.verify_token(USER_TOKEN) is None
    assert len(netbox.requests) == 2


async def test_failed_background_check_keeps_token_and_backs_off(
    passthrough_config, netbox, clock
):
    netbox.get("authentication-check", payload=USER)
    netbox.get("authentication-check", status=503, payload={"detail": "down"})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)
    await verifier.verify_token(USER_TOKEN)

    # NetBox is down: the token keeps working, re-checked once per timeout.
    clock[0] += 61
    for _ in range(3):
        assert await verifier.verify_token(USER_TOKEN) is not None
        await background_checks(verifier)
    assert len(netbox.requests) == 2

    clock[0] += verifier.config.timeout
    assert await verifier.verify_token(USER_TOKEN) is not None
    await background_checks(verifier)
    assert len(netbox.requests) == 3


async def test_token_past_stale_ttl_waits_for_check(passthrough_config, netbox, clock):
    netbox.get("authentication-check", payload=USER)
    netbox.get("authentication-check", status=503, payload={"detail": "down"})
    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=netbox.transport, ttl=60, stale_ttl=600
    )
    await verifier.verify_token(USER_TOKEN)

    clock[0] += 601
    with pytest.raises(NetBoxUnavailableError):
        await verifier.verify_token(USER_TOKEN)


async def test_verifier_cache_is_bounded(passthrough_config, netbox):
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=netbox.transport, max_entries=2
    )

    for token in ("nbt_a.x", "nbt_b.x", "nbt_c.x"):
        await verifier.verify_token(token)

    assert len(verifier._cache) == 2


async def test_verifier_falls_back_to_users_config(passthrough_config, netbox):
    netbox.get("authentication-check", status=404, payload={"detail": "Not found."})
    netbox.get("users/config", payload={})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    access_token = await verifier.verify_token(USER_TOKEN)

    assert access_token is not None
    assert access_token.client_id == f"token:{token_fingerprint(USER_TOKEN)}"


async def test_users_config_username_is_not_trusted(passthrough_config, netbox):
    # users/config returns the user's own writable preferences.
    netbox.get("authentication-check", status=404, payload={"detail": "Not found."})
    netbox.get("users/config", payload={"username": "admin"})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    access_token = await verifier.verify_token(USER_TOKEN)

    assert access_token is not None
    assert access_token.client_id == f"token:{token_fingerprint(USER_TOKEN)}"


async def test_client_id_survives_a_rename(passthrough_config, netbox, clock):
    netbox.get("authentication-check", payload=USER)
    netbox.get("authentication-check", payload={**USER, "username": "alice.smith"})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)
    before = await verifier.verify_token(USER_TOKEN)

    # An admin renames the user; the background re-check sees the new name.
    clock[0] += 61
    await verifier.verify_token(USER_TOKEN)
    await background_checks(verifier)
    after = await verifier.verify_token(USER_TOKEN)

    assert len(netbox.requests) == 2
    assert before is not None
    assert after is not None
    assert before.client_id == after.client_id == "user:7"


@pytest.mark.parametrize("user_id", [None, "7", True, 7.0])
async def test_client_id_needs_an_integer_user_id(passthrough_config, netbox, user_id):
    netbox.get("authentication-check", payload={**USER, "id": user_id})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    access_token = await verifier.verify_token(USER_TOKEN)

    assert access_token is not None
    assert access_token.client_id == f"token:{token_fingerprint(USER_TOKEN)}"


# ------------------------------------------------------- provider choice


def transport(kind: str = "http", bearer: str | None = None) -> TransportConfig:
    return TransportConfig(transport_type=kind, http_bearer_token=bearer)


def test_provider_passthrough(passthrough_config):
    provider = build_auth_provider(passthrough_config, transport("http"))
    assert isinstance(provider, NetBoxTokenVerifier)


@pytest.mark.parametrize("kind", ["stdio", "bogus"])
def test_provider_passthrough_needs_http(passthrough_config, kind):
    with pytest.raises(ValueError, match="MCP_TRANSPORT=http or sse"):
        build_auth_provider(passthrough_config, transport(kind))


def test_provider_passthrough_conflicts_with_bearer(passthrough_config):
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_auth_provider(passthrough_config, transport("http", bearer="shared"))


def test_provider_static_bearer(config):
    provider = build_auth_provider(config, transport("http", bearer="shared"))
    assert isinstance(provider, StaticTokenVerifier)


def test_provider_none(config):
    assert build_auth_provider(config, transport("http")) is None


# ------------------------------------------------------------ tool calls


def fake_access_token(monkeypatch, token: str | None) -> None:
    value = AccessToken(token=token, client_id="alice", scopes=[]) if token else None
    monkeypatch.setattr(netbox_client, "get_access_token", lambda: value)


async def test_tool_uses_callers_token(
    passthrough_config, netbox, client_for, monkeypatch
):
    fake_access_token(monkeypatch, USER_TOKEN)
    netbox.get("status", payload={"netbox-version": "4.5.0"})
    netbox.get("authentication-check", payload=USER)

    async with client_for(passthrough_config) as client:
        result = await client.call_tool("netbox_get_status", {})

    assert result.data["user"]["username"] == "alice"
    assert set(auth_headers(netbox)) == {f"Bearer {USER_TOKEN}"}


async def test_tool_without_token_never_uses_service_token(
    passthrough_config, netbox, client_for, monkeypatch
):
    fake_access_token(monkeypatch, None)

    async with client_for(passthrough_config) as client:
        with pytest.raises(ToolError, match="No NetBox token on this request"):
            await client.call_tool("netbox_get_status", {})

    assert netbox.requests == []


async def test_tool_uses_server_token_without_passthrough(config, netbox, client_for):
    netbox.get("status", payload={"netbox-version": "4.5.0"})
    netbox.get("authentication-check", payload=USER)

    async with client_for(config) as client:
        await client.call_tool("netbox_get_status", {})

    assert set(auth_headers(netbox)) == {f"Bearer {config.token}"}


# ------------------------------------------------------------- over HTTP


def http_server(config: NetBoxConfig, netbox) -> FastMCP:
    mcp = FastMCP("test", auth=NetBoxTokenVerifier(config, transport=netbox.transport))
    register_tools(mcp, config, netbox.transport)
    return mcp


async def test_http_request_runs_as_caller(passthrough_config, netbox):
    netbox.get("status", payload={"netbox-version": "4.5.0"})
    netbox.get("authentication-check", payload=USER)
    mcp = http_server(passthrough_config, netbox)

    async with asgi_client(
        mcp, headers={"Authorization": f"Bearer {USER_TOKEN}"}
    ) as client:
        result = await client.call_tool("netbox_get_status", {})

    assert result.data["user"]["username"] == "alice"
    assert set(auth_headers(netbox)) == {f"Bearer {USER_TOKEN}"}


async def post_tools_list(mcp: FastMCP, headers: dict[str, str]) -> httpx2.Response:
    """Send a raw MCP request, to see the HTTP status the auth layer returns."""
    async with (
        asgi_server(mcp) as server,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=server.app), base_url="http://127.0.0.1"
        ) as http,
    ):
        return await http.post(
            "/mcp",
            headers={**headers, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer nbt_bad.x"},
    ],
)
async def test_http_request_without_valid_token_is_rejected(
    passthrough_config, netbox, headers
):
    netbox.get("authentication-check", status=403, payload={"detail": "Invalid token"})

    response = await post_tools_list(http_server(passthrough_config, netbox), headers)

    assert response.status_code == 401
    assert f"Bearer {SERVICE_TOKEN}" not in auth_headers(netbox)


async def test_http_request_while_netbox_is_down_gets_503(passthrough_config, netbox):
    netbox.get("authentication-check", status=502, payload={"detail": "Bad Gateway"})

    response = await post_tools_list(
        http_server(passthrough_config, netbox),
        {"Authorization": f"Bearer {USER_TOKEN}"},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"] == "temporarily_unavailable"


# ------------------------------------------------------- per-caller state


async def test_verifier_reports_missing_api_as_unavailable(passthrough_config, netbox):
    netbox.get("authentication-check", status=404, payload={"detail": "Not found."})
    netbox.get("users/config", status=404, payload={"detail": "Not found."})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    with pytest.raises(NetBoxUnavailableError, match="NETBOX_URL"):
        await verifier.verify_token(USER_TOKEN)


async def test_explicit_empty_token_is_refused(passthrough_config, netbox):
    with pytest.raises(MissingTokenError):
        async with NetBoxClient(
            passthrough_config, token="", transport=netbox.transport
        ):
            pass
    assert netbox.requests == []


async def test_branch_lookup_is_not_shared_between_callers(netbox):
    config = NetBoxConfig(
        netbox_url="https://netbox.test", token_passthrough=True, branching_enabled=True
    )
    branches = "plugins/branching/branches"
    netbox.get(branches, payload={"results": [{"schema_id": "abcd1234"}]})
    netbox.get(branches, payload={"results": []})

    async with NetBoxClient(config, token=USER_TOKEN, transport=netbox.transport) as nb:
        assert await nb.resolve_branch("secret-change") == "abcd1234"
        assert await nb.resolve_branch("secret-change") == "abcd1234"
    async with NetBoxClient(
        config, token="nbt_bob.x", transport=netbox.transport
    ) as nb:
        with pytest.raises(ValueError, match="not found"):
            await nb.resolve_branch("secret-change")

    assert len(netbox.sent("GET")) == 2


def object_types_page(*models: str) -> dict:
    return {
        "next": None,
        "results": [
            {
                "is_plugin_model": True,
                "app_label": "netbox_dns",
                "model": model,
                "rest_api_endpoint": f"/api/plugins/netbox-dns/{model}s/",
            }
            for model in models
        ],
    }


async def test_plugin_discovery_merges_each_callers_view(netbox):
    config = NetBoxConfig(
        netbox_url="https://netbox.test", token_passthrough=True, plugin_discovery=True
    )
    netbox.get("core/object-types", payload=object_types_page("zone", "record"))
    netbox.get("core/object-types", payload=object_types_page())

    async with NetBoxClient(config, token=USER_TOKEN, transport=netbox.transport) as nb:
        assert await nb.discover_plugin_types() == 2
    # A caller who may see no plugin types runs its own discovery, which must
    # not hide the types the first caller found.
    async with NetBoxClient(
        config, token="nbt_bob.x", transport=netbox.transport
    ) as nb:
        assert await nb.discover_plugin_types() == 2

    assert len(netbox.sent("GET")) == 2
    assert set(plugin_object_types()) == {"netbox_dns.zone", "netbox_dns.record"}


async def test_plugin_rediscovery_replaces_types_without_passthrough(config, netbox):
    config.plugin_discovery = True
    netbox.get("core/object-types", payload=object_types_page("zone"))
    netbox.get("core/object-types", payload=object_types_page())

    async with NetBoxClient(config, transport=netbox.transport) as nb:
        assert await nb.discover_plugin_types() == 1
        assert await nb.discover_plugin_types(force=True) == 0


async def rate_limit_key(monkeypatch, verifier, token) -> str:
    access_token = await verifier.verify_token(token) if token else None
    monkeypatch.setattr(auth, "get_access_token", lambda: access_token)
    return rate_limit_client_id(None)


async def test_rate_limit_is_per_netbox_user(passthrough_config, netbox, monkeypatch):
    netbox.get("authentication-check", payload=USER)
    netbox.get("authentication-check", payload=USER)
    netbox.get("authentication-check", payload={"id": 8, "username": "bob"})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    alice = await rate_limit_key(monkeypatch, verifier, USER_TOKEN)
    alice_other_token = await rate_limit_key(monkeypatch, verifier, "nbt_alice2.x")
    bob = await rate_limit_key(monkeypatch, verifier, "nbt_bob.x")

    assert alice == alice_other_token == "user:7"
    assert bob == "user:8"


async def test_rate_limit_falls_back_to_token_without_user_id(
    passthrough_config, netbox, monkeypatch
):
    netbox.get("authentication-check", status=404, payload={"detail": "Not found."})
    netbox.get("users/config", payload={})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    first = await rate_limit_key(monkeypatch, verifier, USER_TOKEN)
    second = await rate_limit_key(monkeypatch, verifier, "nbt_b.x")

    assert first.startswith("token:")
    assert first != second
    assert USER_TOKEN not in first
    assert await rate_limit_key(monkeypatch, verifier, None) == "global"


# ---------------------------------------------------------- discovery state


@pytest.fixture
def discovery_config() -> NetBoxConfig:
    return NetBoxConfig(
        netbox_url="https://netbox.test", token_passthrough=True, plugin_discovery=True
    )


async def discover(config, netbox, token) -> int:
    async with NetBoxClient(config, token=token, transport=netbox.transport) as nb:
        return await nb.discover_plugin_types()


async def test_discovery_refused_for_a_token_is_not_retried(discovery_config, netbox):
    netbox.get("core/object-types", status=403, payload={"detail": "denied"})
    netbox.get("core/object-types", payload=object_types_page("zone"))

    assert await discover(discovery_config, netbox, "nbt_bob.x") == 0
    assert await discover(discovery_config, netbox, "nbt_bob.x") == 0
    assert len(netbox.requests) == 1
    # Another caller still runs its own discovery.
    assert await discover(discovery_config, netbox, USER_TOKEN) == 1


async def test_discovery_outage_is_retried(discovery_config, netbox):
    netbox.get("core/object-types", status=500, payload={"detail": "boom"})
    assert await discover(discovery_config, netbox, USER_TOKEN) == 0

    netbox.routes.clear()
    netbox.get("core/object-types", payload=object_types_page("zone"))
    assert await discover(discovery_config, netbox, USER_TOKEN) == 1


async def test_discovered_caller_does_not_wait_for_another(discovery_config, netbox):
    netbox.get("core/object-types", payload=object_types_page("zone"))
    await discover(discovery_config, netbox, USER_TOKEN)

    # A forced rediscovery with the same token is in progress and holds the lock.
    alice_key = ("https://netbox.test", token_fingerprint(USER_TOKEN))
    NetBoxClient._discovery_locks[alice_key] = lock = asyncio.Lock()
    async with lock:
        count = await asyncio.wait_for(
            discover(discovery_config, netbox, USER_TOKEN), timeout=1
        )

    assert count == 1


async def test_new_caller_does_not_wait_for_another(discovery_config, netbox):
    netbox.get("core/object-types", payload=object_types_page("zone"))

    # Bob's first discovery is in progress; Alice's first one runs anyway.
    bob_key = ("https://netbox.test", token_fingerprint("nbt_bob.x"))
    NetBoxClient._discovery_locks[bob_key] = lock = asyncio.Lock()
    async with lock:
        count = await asyncio.wait_for(
            discover(discovery_config, netbox, USER_TOKEN), timeout=1
        )

    assert count == 1


async def test_resolve_skips_discovery_for_known_types(discovery_config, netbox):
    async with NetBoxClient(
        discovery_config, token=USER_TOKEN, transport=netbox.transport
    ) as nb:
        assert (await nb.resolve("dcim.device"))[0] == "dcim.device"

    assert netbox.requests == []


async def test_resolve_discovers_unknown_type_once(discovery_config, netbox):
    netbox.get("core/object-types", payload=object_types_page("zone"))

    async with NetBoxClient(
        discovery_config, token=USER_TOKEN, transport=netbox.transport
    ) as nb:
        assert (await nb.resolve("netbox_dns.zone"))[0] == "netbox_dns.zone"
        for _ in range(2):
            with pytest.raises(ValueError, match="Unknown object type"):
                await nb.resolve("netbox_dns.nope")

    assert len(netbox.requests) == 1


async def test_forced_rediscovery_drops_uninstalled_types(discovery_config, netbox):
    netbox.get("core/object-types", payload=object_types_page("zone", "record"))
    netbox.get("core/object-types", payload=object_types_page("zone"))
    assert await discover(discovery_config, netbox, USER_TOKEN) == 2

    # The plugin dropped "record"; Bob forces a rediscovery.
    async with NetBoxClient(
        discovery_config, token="nbt_bob.x", transport=netbox.transport
    ) as nb:
        assert await nb.discover_plugin_types(force=True) == 1

    assert set(plugin_object_types()) == {"netbox_dns.zone"}


async def test_types_only_another_caller_sees_come_back(discovery_config, netbox):
    netbox.get("core/object-types", payload=object_types_page("zone"))
    netbox.get("core/object-types", payload=object_types_page())
    netbox.get("core/object-types", payload=object_types_page("zone"))
    assert await discover(discovery_config, netbox, USER_TOKEN) == 1

    # Bob may see no plugin types, so his forced rediscovery empties the list.
    async with NetBoxClient(
        discovery_config, token="nbt_bob.x", transport=netbox.transport
    ) as nb:
        assert await nb.discover_plugin_types(force=True) == 0

    # Alice's next call for the type rediscovers it with her token.
    async with NetBoxClient(
        discovery_config, token=USER_TOKEN, transport=netbox.transport
    ) as nb:
        assert (await nb.resolve("netbox_dns.zone"))[0] == "netbox_dns.zone"


async def test_per_caller_caches_are_bounded(netbox, monkeypatch):
    monkeypatch.setattr(netbox_client, "CALLER_CACHE_LIMIT", 2)
    config = NetBoxConfig(
        netbox_url="https://netbox.test", token_passthrough=True, branching_enabled=True
    )
    netbox.get(
        "plugins/branching/branches", payload={"results": [{"schema_id": "abcd1234"}]}
    )

    for token in ("nbt_a.x", "nbt_b.x", "nbt_c.x"):
        async with NetBoxClient(config, token=token, transport=netbox.transport) as nb:
            await nb.resolve_branch("feature")

    assert len(NetBoxClient._branch_cache) == 2


# ------------------------------------------------- outage answers and logs


def respond_with(status: int, headers: dict[str, str] | None = None):
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"detail": "busy"}, headers=headers)

    return httpx2.MockTransport(handle)


async def test_api_error_keeps_netbox_retry_after(config):
    transport = respond_with(429, {"Retry-After": "60"})

    with pytest.raises(NetBoxAPIError) as excinfo:
        async with NetBoxClient(config, transport=transport) as nb:
            await nb.get("dcim/devices")

    assert excinfo.value.retry_after == 60


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"Retry-After": "60"}, "60"), ({"Retry-After": "99999"}, "3600"), (None, "5")],
)
async def test_503_passes_on_netbox_retry_after(passthrough_config, headers, expected):
    transport = respond_with(429, headers)
    mcp = FastMCP(
        "test", auth=NetBoxTokenVerifier(passthrough_config, transport=transport)
    )

    response = await post_tools_list(mcp, {"Authorization": f"Bearer {USER_TOKEN}"})

    assert response.status_code == 503
    assert response.headers["retry-after"] == expected


@pytest.mark.parametrize("status", [404, 502])
async def test_503_body_does_not_reveal_server_details(
    passthrough_config, netbox, status
):
    netbox.get("authentication-check", status=status, payload={"detail": "x"})
    netbox.get("users/config", status=status, payload={"detail": "x"})

    response = await post_tools_list(
        http_server(passthrough_config, netbox),
        {"Authorization": f"Bearer {USER_TOKEN}"},
    )

    assert response.status_code == 503
    assert response.json()["error_description"] == (
        "NetBox could not verify the token; retry later"
    )
    assert "NETBOX_URL" not in response.text
    assert str(status) not in response.text


async def test_rejected_token_is_logged_at_debug(passthrough_config, netbox, caplog):
    netbox.get("authentication-check", status=403, payload={"detail": "Invalid token"})
    verifier = NetBoxTokenVerifier(passthrough_config, transport=netbox.transport)

    with caplog.at_level(logging.DEBUG, logger="netbox_mcp.auth"):
        assert await verifier.verify_token(USER_TOKEN) is None

    (record,) = [r for r in caplog.records if r.name == "netbox_mcp.auth"]
    assert record.levelno == logging.DEBUG
    assert "Authorization header" in record.getMessage()
    assert USER_TOKEN not in record.getMessage()


async def test_server_token_retries_refused_discovery(config, netbox):
    config.plugin_discovery = True
    netbox.get("core/object-types", status=403, payload={"detail": "denied"})
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        assert await nb.discover_plugin_types() == 0

    # An admin grants the permission: the next call picks the types up.
    netbox.routes.clear()
    netbox.get("core/object-types", payload=object_types_page("zone"))
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        assert await nb.discover_plugin_types() == 1


async def test_token_check_has_its_own_short_timeout(passthrough_config):
    async def hang(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json=USER)

    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=httpx2.MockTransport(hang), timeout=0.05
    )

    with pytest.raises(NetBoxUnavailableError, match="TimeoutError"):
        await asyncio.wait_for(verifier.verify_token(USER_TOKEN), timeout=2)


def test_token_check_timeout_never_exceeds_netbox_timeout():
    config = NetBoxConfig(netbox_url="https://netbox.test", timeout=3)

    assert NetBoxTokenVerifier(config).config.timeout == 3
    assert NetBoxTokenVerifier(config, timeout=1).config.timeout == 1
    assert config.timeout == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("60", 60),
        (" 2.5 ", 2.5),
        ("inf", None),
        ("1e999", None),
        ("nan", None),
        ("-1", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        (None, None),
    ],
)
def test_parse_retry_after(raw, expected):
    assert parse_retry_after(raw) == expected


async def test_503_ignores_infinite_retry_after(passthrough_config):
    transport = respond_with(503, {"Retry-After": "inf"})
    mcp = FastMCP(
        "test", auth=NetBoxTokenVerifier(passthrough_config, transport=transport)
    )

    response = await post_tools_list(mcp, {"Authorization": f"Bearer {USER_TOKEN}"})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


async def test_token_check_timeout_covers_the_fallback_request(passthrough_config):
    async def slow(request: httpx2.Request) -> httpx2.Response:
        # Each request fits the limit alone; together they do not.
        await asyncio.sleep(0.3)
        if request.url.path == "/api/authentication-check/":
            return httpx2.Response(404, json={"detail": "Not found."})
        return httpx2.Response(200, json={})

    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=httpx2.MockTransport(slow), timeout=0.5
    )

    with pytest.raises(NetBoxUnavailableError, match="TimeoutError"):
        await verifier.verify_token(USER_TOKEN)


async def test_verifier_forgets_expired_tokens(passthrough_config, netbox, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(auth.time, "monotonic", lambda: now)
    netbox.get("authentication-check", payload=USER)
    verifier = NetBoxTokenVerifier(
        passthrough_config, transport=netbox.transport, ttl=60, stale_ttl=120
    )

    await verifier.verify_token("nbt_once.x")
    now += 121
    await verifier.verify_token(USER_TOKEN)

    cached = [entry[0].token for entry in verifier._cache.values()]
    assert cached == [USER_TOKEN]
