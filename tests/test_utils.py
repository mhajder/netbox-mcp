import pytest

from netbox_mcp.utils import env_bool
from netbox_mcp.utils import http_host_from_env
from netbox_mcp.utils import http_port_from_env
from netbox_mcp.utils import parse_bool
from netbox_mcp.utils import transport_from_env


@pytest.mark.parametrize(
    ("val", "default", "expected"),
    [
        (None, True, True),
        (None, False, False),
        ("1", False, True),
        ("true", False, True),
        ("yes", False, True),
        ("on", False, True),
        ("TrUe", False, True),
        ("YES", False, True),
        ("ON", False, True),
        ("1 ", False, True),
        ("0", True, False),
        ("false", True, False),
        ("no", True, False),
        ("False", True, False),
        # Blank means "unset", so the default wins rather than False.
        ("", True, True),
        ("", False, False),
        ("  ", True, True),
        ("  ", False, False),
    ],
)
def test_parse_bool(val, default, expected):
    assert parse_bool(val, default) is expected


@pytest.mark.parametrize("val", ["random", "enabled", "enable", "y", "2"])
@pytest.mark.parametrize("default", [True, False])
def test_parse_bool_rejects_unknown_values(val, default):
    """A typo must not read as False: READ_ONLY_MODE=enable would expose writes."""
    with pytest.raises(ValueError, match=f"got {val!r}"):
        parse_bool(val, default)


def test_env_bool_names_the_variable(monkeypatch):
    monkeypatch.setenv("NETBOX_VERIFY_SSL", "y")
    with pytest.raises(ValueError, match="NETBOX_VERIFY_SSL must be one of"):
        env_bool("NETBOX_VERIFY_SSL", default=True)


def test_env_bool_treats_blank_as_unset(monkeypatch):
    monkeypatch.setenv("NETBOX_VERIFY_SSL", " ")
    assert env_bool("NETBOX_VERIFY_SSL", default=True) is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "stdio"),
        ("stdio", "stdio"),
        ("SSE", "sse"),
        ("HTTP", "http"),
        # Not stripped: main() runs stdio for this, as before.
        (" http ", " http "),
        # FastMCP's name for the streamable HTTP transport.
        ("streamable-http", "http"),
    ],
)
def test_transport_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("MCP_TRANSPORT", raw)
    assert transport_from_env() == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "127.0.0.1"),
        ("0.0.0.0", "0.0.0.0"),  # noqa: S104
        # Kept as is: uvicorn binds every interface for an empty host.
        ("", ""),
        ("[::]", "::"),
        ("[fd00::5]", "fd00::5"),
    ],
)
def test_http_host_from_env(monkeypatch, raw, expected):
    """The server and the healthcheck share this; uvicorn needs bare IPv6."""
    if raw is None:
        monkeypatch.delenv("MCP_HTTP_HOST", raising=False)
    else:
        monkeypatch.setenv("MCP_HTTP_HOST", raw)
    assert http_host_from_env() == expected


@pytest.mark.parametrize(("raw", "expected"), [(None, 8000), ("9000", 9000)])
def test_http_port_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MCP_HTTP_PORT", raising=False)
    else:
        monkeypatch.setenv("MCP_HTTP_PORT", raw)
    assert http_port_from_env() == expected


@pytest.mark.parametrize("raw", ["", "auto"])
def test_http_port_from_env_rejects_non_integers(monkeypatch, raw):
    monkeypatch.setenv("MCP_HTTP_PORT", raw)
    with pytest.raises(ValueError, match="invalid literal for int"):
        http_port_from_env()
