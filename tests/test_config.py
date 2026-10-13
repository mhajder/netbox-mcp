"""Settings read from the environment by get_*_config_from_env."""

import pytest

from netbox_mcp.netbox_client import get_netbox_config_from_env

SETTINGS = (
    "NETBOX_TOKEN_PASSTHROUGH",
    "NETBOX_VERIFY_SSL",
    "NETBOX_BRANCHING_ENABLED",
    "NETBOX_PLUGIN_DISCOVERY",
    "READ_ONLY_MODE",
    "DRY_RUN_MODE",
    "RATE_LIMIT_ENABLED",
    "TOOL_SEARCH_ENABLED",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from defaults, whatever the developer's .env holds."""
    for name in (
        *SETTINGS,
        "NETBOX_URL",
        "NETBOX_TOKEN",
        "NETBOX_BRANCH",
        "NETBOX_TIMEOUT",
        "RESPONSE_MAX_SIZE",
        "RATE_LIMIT_MAX_REQUESTS",
        "RATE_LIMIT_WINDOW_MINUTES",
        "TOOL_SEARCH_STRATEGY",
        "TOOL_SEARCH_MAX_RESULTS",
        "MCP_TRANSPORT",
        "MCP_HTTP_HOST",
        "MCP_HTTP_PORT",
        "MCP_HTTP_BEARER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETBOX_URL", "https://netbox.invalid")


@pytest.mark.parametrize("name", SETTINGS)
def test_unknown_bool_setting_stops_startup(monkeypatch, name):
    """A typo must not turn read-only mode or TLS verification off."""
    monkeypatch.setenv(name, "enabled")
    with pytest.raises(ValueError, match=f"{name} must be one of"):
        get_netbox_config_from_env()


def test_verify_ssl_unknown_value_is_not_read_as_false(monkeypatch):
    monkeypatch.setenv("NETBOX_VERIFY_SSL", "y")
    with pytest.raises(ValueError, match="NETBOX_VERIFY_SSL"):
        get_netbox_config_from_env()
