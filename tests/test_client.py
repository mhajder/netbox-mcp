import asyncio

import httpx2
import pytest

from netbox_mcp import retry
from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import AmbiguousWriteError
from netbox_mcp.netbox_client import NetBoxAPIError
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.netbox_client import auth_header


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("nbt_abc.secret", "Bearer nbt_abc.secret"),
        ("0123456789abcdef", "Token 0123456789abcdef"),
    ],
)
def test_auth_scheme_follows_token_version(token, expected):
    assert auth_header(token) == {"Authorization": expected}


async def test_fallback_endpoint_used_on_404(config, netbox):
    netbox.get("core/object-types", status=404, payload={"detail": "nope"})
    netbox.get("extras/object-types", payload={"results": []})
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        result = await nb.get("core/object-types", fallback_path="extras/object-types")
    assert result == {"results": []}


async def test_error_keeps_netbox_body(config, netbox):
    body = {"name": ["This field is required."]}
    netbox.post("dcim/sites", status=400, payload=body)
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        with pytest.raises(NetBoxAPIError) as info:
            await nb.post("dcim/sites", {})
    assert info.value.status == 400
    assert info.value.body == body


async def test_branch_name_resolved_to_schema_id(config, netbox):
    config.branching_enabled = True
    netbox.get(
        "plugins/branching/branches",
        payload={"results": [{"name": "feature", "schema_id": "td5smq0f"}]},
    )
    netbox.get("dcim/sites", payload={"results": []})
    async with NetBoxClient(config, branch="feature", transport=netbox.transport) as nb:
        await nb.get("dcim/sites")

    lookup, site_call = netbox.sent("GET")
    assert "X-NetBox-Branch" not in lookup.headers
    assert site_call.headers["X-NetBox-Branch"] == "td5smq0f"


async def test_unknown_branch_rejected(config, netbox):
    config.branching_enabled = True
    netbox.get("plugins/branching/branches", payload={"results": []})
    async with NetBoxClient(
        config, branch="missing branch", transport=netbox.transport
    ) as nb:
        with pytest.raises(ValueError, match="not found"):
            await nb.get("dcim/sites")


def _failing_transport(exc: Exception) -> httpx2.MockTransport:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise exc

    return httpx2.MockTransport(handler)


async def test_read_timeout_on_write_is_ambiguous(config):
    transport = _failing_transport(httpx2.ReadTimeout("slow"))
    async with NetBoxClient(config, transport=transport) as nb:
        with pytest.raises(AmbiguousWriteError, match="MAY have been applied"):
            await nb.post("dcim/sites", {"name": "x"})


async def test_connect_error_on_write_is_safe_to_retry(config):
    transport = _failing_transport(httpx2.ConnectError("refused"))
    async with NetBoxClient(config, transport=transport) as nb:
        with pytest.raises(httpx2.ConnectError):
            await nb.post("dcim/sites", {"name": "x"})


async def test_read_timeout_on_read_is_plain(config):
    transport = _failing_transport(httpx2.ReadTimeout("slow"))
    async with NetBoxClient(config, transport=transport) as nb:
        with pytest.raises(httpx2.ReadTimeout):
            await nb.get("dcim/sites")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("4.5.2", (4, 5, 2)), ("4.3.0-Docker-3.3.0", (4, 3, 0)), ("", None)],
)
async def test_version_parsing(config, netbox, raw, expected):
    netbox.get("status", payload={"netbox-version": raw})
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        assert await nb.version() == expected


async def _run_retry(method: str, statuses: list[int]) -> int:
    responses = iter(statuses)
    calls = 0

    def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(next(responses))

    transport = retry.RetryTransport(httpx2.MockTransport(handler), owns_wrapped=True)
    await transport.handle_async_request(
        httpx2.Request(method, "https://netbox.test/api/dcim/sites/")
    )
    return calls


async def test_retry_get_on_5xx():
    assert await _run_retry("GET", [502, 502, 200]) == 3


async def test_no_retry_post_on_5xx():
    assert await _run_retry("POST", [502, 200]) == 1


async def test_retry_post_on_429():
    assert await _run_retry("POST", [429, 201]) == 2


async def test_retry_gives_up_after_max_attempts():
    assert await _run_retry("GET", [503, 503, 503, 200]) == retry.MAX_ATTEMPTS


async def test_empty_error_body_still_raises(config, netbox):
    netbox.get("dcim/sites", status=502, payload=None)
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        with pytest.raises(NetBoxAPIError) as info:
            await nb.get("dcim/sites")
    assert info.value.status == 502


def _redirecting_transport(status: int) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.scheme == "http":
            location = str(request.url.copy_with(scheme="https"))
            return httpx2.Response(status, headers={"Location": location})
        return httpx2.Response(200, json={"method": request.method})

    return httpx2.MockTransport(handler)


async def test_redirect_followed_for_reads(config):
    config.netbox_url = "http://netbox.test"
    async with NetBoxClient(config, transport=_redirecting_transport(301)) as nb:
        assert await nb.get("dcim/sites") == {"method": "GET"}


async def test_redirect_that_drops_a_write_is_rejected(config):
    config.netbox_url = "http://netbox.test"
    async with NetBoxClient(config, transport=_redirecting_transport(302)) as nb:
        with pytest.raises(ValueError, match="NOT applied"):
            await nb.post("dcim/sites", {"name": "x"})


async def test_method_preserving_redirect_keeps_the_write(config):
    config.netbox_url = "http://netbox.test"
    async with NetBoxClient(config, transport=_redirecting_transport(307)) as nb:
        assert await nb.post("dcim/sites", {"name": "x"}) == {"method": "POST"}


def _slow_transport() -> httpx2.MockTransport:
    async def handler(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(1)
        return httpx2.Response(200, json={})

    return httpx2.MockTransport(handler)


async def test_overall_timeout_on_read(config):
    config.timeout = 0.05
    async with NetBoxClient(config, transport=_slow_transport()) as nb:
        with pytest.raises(TimeoutError, match=r"within 0\.05s"):
            await nb.get("dcim/sites")


async def test_overall_timeout_on_write_is_ambiguous(config):
    config.timeout = 0.05
    async with NetBoxClient(config, transport=_slow_transport()) as nb:
        with pytest.raises(AmbiguousWriteError):
            await nb.post("dcim/sites", {"name": "x"})


async def test_undecodable_write_response_is_ambiguous(config):
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            201, headers={"Content-Encoding": "gzip"}, content=b"not gzip"
        )

    async with NetBoxClient(config, transport=httpx2.MockTransport(handler)) as nb:
        with pytest.raises(AmbiguousWriteError, match="DecodingError"):
            await nb.post("dcim/sites", {"name": "x"})


@pytest.mark.parametrize(
    "exc", [httpx2.InvalidURL("bad"), httpx2.ReadTimeout("slow"), TimeoutError()]
)
async def test_version_lookup_degrades(config, exc):
    async with NetBoxClient(config, transport=_failing_transport(exc)) as nb:
        assert await nb.version() is None


def test_shared_transport_is_reused():
    assert retry.shared_transport(True) is retry.shared_transport(True)
    assert retry.shared_transport(True) is not retry.shared_transport(False)


async def test_client_does_not_close_its_transport(config):
    class TrackingTransport(httpx2.MockTransport):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    transport = TrackingTransport(lambda _r: httpx2.Response(200, json={}))
    async with NetBoxClient(config, transport=transport) as nb:
        await nb.get("dcim/sites")
    assert transport.closed is False


def test_branch_ignored_when_branching_disabled(config):
    assert NetBoxClient(config, branch="feature").branch is None


def test_default_branch_requires_branching():
    with pytest.raises(ValueError, match="NETBOX_BRANCHING_ENABLED"):
        NetBoxConfig(netbox_url="https://netbox.test", default_branch="feature")


def test_disabled_feature_settings_are_not_validated():
    """Leftover values of a disabled feature must not stop the server."""
    config = NetBoxConfig(
        netbox_url="https://netbox.test",
        rate_limit_max_requests=0,
        tool_search_max_results=0,
    )
    assert config.rate_limit_max_requests == 0


async def test_no_authorization_header_without_a_token(netbox):
    config = NetBoxConfig(netbox_url="https://netbox.test")
    netbox.get("status", payload={})
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        await nb.get("status")
    assert "authorization" not in netbox.requests[0].headers


async def test_request_outside_context_is_refused(config, netbox):
    nb = NetBoxClient(config, transport=netbox.transport)
    with pytest.raises(RuntimeError, match="async context manager"):
        await nb.get("dcim/sites")
    assert not netbox.requests


async def test_non_json_error_body_is_kept_as_text(config):
    page = "<html>" + "x" * 3000 + "</html>"

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(502, text=page)

    async with NetBoxClient(config, transport=httpx2.MockTransport(handler)) as nb:
        with pytest.raises(NetBoxAPIError) as info:
            await nb.get("dcim/sites")
    assert info.value.status == 502
    assert info.value.body == page[:2000]


async def test_branch_lookup_without_branching_plugin(config, netbox):
    config.branching_enabled = True
    netbox.get("plugins/branching/branches", status=404, payload={"detail": "x"})
    async with NetBoxClient(config, branch="feature", transport=netbox.transport) as nb:
        with pytest.raises(ValueError, match="needs the netbox-branching plugin"):
            await nb.get("dcim/sites")
    assert len(netbox.requests) == 1


async def test_branch_lookup_error_is_raised(config, netbox):
    config.branching_enabled = True
    netbox.get("plugins/branching/branches", status=403, payload={"detail": "no"})
    async with NetBoxClient(config, branch="feature", transport=netbox.transport) as nb:
        with pytest.raises(NetBoxAPIError) as info:
            await nb.get("dcim/sites")
    assert info.value.status == 403
    assert len(netbox.requests) == 1


async def test_unknown_branch_name_shaped_like_schema_id_is_used_as_is(config, netbox):
    config.branching_enabled = True
    netbox.get("plugins/branching/branches", payload={"results": []})
    netbox.get("dcim/sites", payload={"results": []})
    async with NetBoxClient(
        config, branch="td5smq0f", transport=netbox.transport
    ) as nb:
        await nb.get("dcim/sites")
    lookup, site_call = netbox.sent("GET")
    assert dict(lookup.query)["name"] == "td5smq0f"
    assert site_call.headers["X-NetBox-Branch"] == "td5smq0f"


async def test_concurrent_discovery_runs_one_scan(config):
    config.plugin_discovery = True
    calls = 0

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        # Let the second caller reach the lock while the scan is running.
        await asyncio.sleep(0.01)
        return httpx2.Response(200, json={"next": None, "results": []})

    async with NetBoxClient(config, transport=httpx2.MockTransport(handler)) as nb:
        counts = await asyncio.gather(
            nb.discover_plugin_types(), nb.discover_plugin_types()
        )
    assert counts == [0, 0]
    assert calls == 1


@pytest.mark.parametrize(
    "row",
    [
        {"is_plugin_model": True, "app_label": "netbox_dns", "model": "zone"},
        {
            "is_plugin_model": True,
            "app_label": "netbox_dns",
            "model": "zone",
            "rest_api_endpoint": "/api/dcim/zones/",
        },
    ],
    ids=["no-endpoint", "outside-plugins"],
)
async def test_discovery_skips_unusable_plugin_rows(config, netbox, row):
    config.plugin_discovery = True
    netbox.get("core/object-types", payload={"next": None, "results": [row]})
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        assert await nb.discover_plugin_types() == 0
        with pytest.raises(ValueError, match="Unknown object type"):
            await nb.resolve("netbox_dns.zone")


async def _retry_delays(monkeypatch, retry_after: str) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(retry, "BASE_BACKOFF", 0.5)
    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    responses = iter([429, 200])

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(next(responses), headers={"Retry-After": retry_after})

    transport = retry.RetryTransport(httpx2.MockTransport(handler), owns_wrapped=False)
    response = await transport.handle_async_request(
        httpx2.Request("GET", "https://netbox.test/api/dcim/sites/")
    )
    assert response.status_code == 200
    return delays


async def test_retry_waits_as_long_as_retry_after_says(monkeypatch):
    assert await _retry_delays(monkeypatch, "2") == [2.0]


async def test_retry_ignores_a_retry_after_beyond_the_cap(monkeypatch):
    """Blocking a tool call for an hour is worse than failing it."""
    assert await _retry_delays(monkeypatch, "3600") == [retry.BASE_BACKOFF]


@pytest.mark.parametrize("owns", [True, False])
async def test_retry_transport_closes_only_what_it_owns(owns):
    class TrackingTransport(httpx2.MockTransport):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    wrapped = TrackingTransport(lambda _r: httpx2.Response(200))
    await retry.RetryTransport(wrapped, owns_wrapped=owns).aclose()
    assert wrapped.closed is owns
