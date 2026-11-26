"""Tests for outbound User-Agent configuration and header injection."""

import pytest

from netbox_mcp.netbox_client import DEFAULT_USER_AGENT
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.netbox_client import get_netbox_config_from_env
from tests.conftest import FakeNetBox


def test_env_user_agent_defaults_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", "https://netbox.test")
    monkeypatch.setenv("NETBOX_TOKEN", "nbt_abc.secret")
    monkeypatch.delenv("NETBOX_USER_AGENT", raising=False)

    assert get_netbox_config_from_env().user_agent is None

    monkeypatch.setenv("NETBOX_USER_AGENT", "  custom-netbox/1.0  ")
    assert get_netbox_config_from_env().user_agent == "custom-netbox/1.0"

    monkeypatch.setenv("NETBOX_USER_AGENT", "   ")
    assert get_netbox_config_from_env().user_agent is None


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, DEFAULT_USER_AGENT),
        ("custom-netbox/1.0", "custom-netbox/1.0"),
    ],
)
async def test_outbound_requests_send_user_agent(
    config, netbox: FakeNetBox, configured: str | None, expected: str
) -> None:
    config.user_agent = configured
    netbox.get("dcim/sites", payload={"results": []})
    async with NetBoxClient(config, transport=netbox.transport) as client:
        await client.get("dcim/sites")

    assert netbox.requests
    assert all(req.headers.get("User-Agent") == expected for req in netbox.requests)
