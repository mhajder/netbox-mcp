"""Settings read from the environment by get_*_config_from_env."""

import pytest

from netbox_mcp.netbox_client import get_netbox_config_from_env
from netbox_mcp.netbox_client import get_transport_config_from_env
from netbox_mcp.sentry_init import init_sentry

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


@pytest.mark.parametrize(
    "name", ["NETBOX_TIMEOUT", "RESPONSE_MAX_SIZE", "NETBOX_TOKEN", "NETBOX_BRANCH"]
)
def test_blank_setting_means_default(monkeypatch, name):
    """A setting declared without a value (NETBOX_TIMEOUT=) must not crash."""
    monkeypatch.setenv(name, "  ")
    config = get_netbox_config_from_env()
    assert config.timeout == 30
    assert config.response_max_size is None
    assert config.token is None
    assert config.default_branch is None


def test_settings_are_stripped(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", " https://netbox.invalid ")
    monkeypatch.setenv("NETBOX_TOKEN", " nbt_abc.def ")
    monkeypatch.setenv("NETBOX_TIMEOUT", " 12.5 ")
    config = get_netbox_config_from_env()
    assert config.netbox_url == "https://netbox.invalid"
    assert config.token == "nbt_abc.def"
    assert config.timeout == 12.5


@pytest.mark.parametrize(
    ("name", "kind"),
    [("NETBOX_TIMEOUT", "a number"), ("RESPONSE_MAX_SIZE", "an integer")],
)
def test_bad_number_names_the_variable(monkeypatch, name, kind):
    monkeypatch.setenv(name, "lots")
    with pytest.raises(ValueError, match=f"{name} must be {kind}, got 'lots'"):
        get_netbox_config_from_env()


def test_whitespace_bearer_token_is_no_token(monkeypatch):
    """A bearer token of spaces must not be accepted as a real token."""
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "   ")
    assert get_transport_config_from_env().http_bearer_token is None


def test_bearer_token_is_stripped(monkeypatch):
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", " secret ")
    assert get_transport_config_from_env().http_bearer_token == "secret"


def test_whitespace_sentry_dsn_leaves_sentry_off(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "  ")
    assert init_sentry() is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RATE_LIMIT_MAX_REQUESTS", "0"),
        ("RATE_LIMIT_WINDOW_MINUTES", "soon"),
        ("TOOL_SEARCH_MAX_RESULTS", "-1"),
        ("TOOL_SEARCH_STRATEGY", "regx"),
    ],
)
def test_disabled_feature_ignores_its_settings(monkeypatch, name, value):
    """A leftover value must not stop a server that never uses the feature."""
    monkeypatch.setenv(name, value)
    config = get_netbox_config_from_env()
    assert config.rate_limit_max_requests == 60
    assert config.rate_limit_window_minutes == 1
    assert config.tool_search_max_results == 5
    assert config.tool_search_strategy == "bm25"


@pytest.mark.parametrize(
    ("enable", "name"),
    [
        ("RATE_LIMIT_ENABLED", "RATE_LIMIT_MAX_REQUESTS"),
        ("RATE_LIMIT_ENABLED", "RATE_LIMIT_WINDOW_MINUTES"),
        ("TOOL_SEARCH_ENABLED", "TOOL_SEARCH_MAX_RESULTS"),
    ],
)
def test_enabled_feature_rejects_zero(monkeypatch, enable, name):
    monkeypatch.setenv(enable, "true")
    monkeypatch.setenv(name, "0")
    with pytest.raises(ValueError, match=f"must be at least 1: {name}=0"):
        get_netbox_config_from_env()


def test_enabled_feature_reads_its_settings(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "10")
    monkeypatch.setenv("TOOL_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TOOL_SEARCH_STRATEGY", " Regex ")
    monkeypatch.setenv("TOOL_SEARCH_MAX_RESULTS", "")
    config = get_netbox_config_from_env()
    assert config.rate_limit_max_requests == 10
    assert config.tool_search_strategy == "regex"
    assert config.tool_search_max_results == 5


def test_strategy_typo_rejected_when_search_enabled(monkeypatch):
    monkeypatch.setenv("TOOL_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TOOL_SEARCH_STRATEGY", "regx")
    with pytest.raises(ValueError, match="TOOL_SEARCH_STRATEGY must be 'bm25' or"):
        get_netbox_config_from_env()


@pytest.mark.parametrize("transport", [None, "stdio"])
def test_port_ignored_under_stdio(monkeypatch, transport):
    """A leftover MCP_HTTP_PORT must not stop stdio or `fastmcp run`."""
    if transport is not None:
        monkeypatch.setenv("MCP_TRANSPORT", transport)
    monkeypatch.setenv("MCP_HTTP_PORT", "auto")
    assert get_transport_config_from_env().http_port == 8000


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_port_parsed_for_http_transports(monkeypatch, transport):
    monkeypatch.setenv("MCP_TRANSPORT", transport)
    monkeypatch.setenv("MCP_HTTP_PORT", "auto")
    with pytest.raises(ValueError, match="MCP_HTTP_PORT must be an integer"):
        get_transport_config_from_env()
