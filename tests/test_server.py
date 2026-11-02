"""In-process tests for server.py's module-level setup and main().

server.py configures everything at import time, so each test imports a fresh
copy under a controlled environment. The subprocess tests in
test_healthcheck.py cover the same module end to end, but coverage does not
see them.
"""

import importlib
import importlib.metadata
import logging
import sys
from unittest.mock import MagicMock

import dotenv
import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware.rate_limiting import SlidingWindowRateLimitingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search import RegexSearchTransform

from netbox_mcp.auth import NetBoxTokenVerifier

MODULE = "netbox_mcp.server"

SERVER_ENV_VARS = (
    "NETBOX_URL",
    "NETBOX_TOKEN",
    "NETBOX_TOKEN_PASSTHROUGH",
    "NETBOX_VERIFY_SSL",
    "NETBOX_TIMEOUT",
    "NETBOX_BRANCHING_ENABLED",
    "NETBOX_BRANCH",
    "NETBOX_PLUGIN_DISCOVERY",
    "READ_ONLY_MODE",
    "DRY_RUN_MODE",
    "DISABLED_TAGS",
    "RESPONSE_MAX_SIZE",
    "RATE_LIMIT_ENABLED",
    "RATE_LIMIT_MAX_REQUESTS",
    "RATE_LIMIT_WINDOW_MINUTES",
    "TOOL_SEARCH_ENABLED",
    "TOOL_SEARCH_STRATEGY",
    "TOOL_SEARCH_MAX_RESULTS",
    "LOG_LEVEL",
    "MCP_TRANSPORT",
    "MCP_HTTP_HOST",
    "MCP_HTTP_PORT",
    "MCP_HTTP_BEARER_TOKEN",
    "SENTRY_DSN",
)


@pytest.fixture
def load_server(monkeypatch):
    """Import a fresh server module with only the given environment."""
    for name in SERVER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # A developer's .env must not leak into the tests.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)

    def factory(**env: str):
        env.setdefault("NETBOX_URL", "https://netbox.test")
        env.setdefault("NETBOX_TOKEN", "nbt_abc.secret")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        sys.modules.pop(MODULE, None)
        module = importlib.import_module(MODULE)
        monkeypatch.setattr(module.mcp, "run", MagicMock())
        return module

    yield factory
    sys.modules.pop(MODULE, None)


async def tool_names(module) -> set[str]:
    return {tool.name for tool in await module.mcp.list_tools()}


async def test_health_route_answers_ok(load_server):
    server = load_server()

    response = await server.health(MagicMock())

    assert response.status_code == 200
    assert response.body == b"ok"


async def test_all_tools_are_exposed_by_default(load_server):
    names = await tool_names(load_server())

    assert {
        "netbox_get_objects",
        "netbox_create_object",
        "netbox_delete_object",
        "netbox_run_script",
    } <= names
    assert "netbox_list_branches" not in names
    assert "search_tools" not in names


async def test_branching_registers_the_branch_tools(load_server):
    names = await tool_names(load_server(NETBOX_BRANCHING_ENABLED="true"))

    assert {"netbox_list_branches", "netbox_create_branch"} <= names


async def test_read_only_mode_hides_write_tools(load_server, caplog):
    caplog.set_level(logging.INFO)
    names = await tool_names(load_server(READ_ONLY_MODE="true"))

    assert "netbox_get_objects" in names
    assert "netbox_create_object" not in names
    assert "netbox_delete_object" not in names
    assert "netbox_run_script" not in names
    assert "Read-only mode is enabled" in caplog.text


async def test_disabled_tags_hide_matching_tools(load_server, caplog):
    caplog.set_level(logging.INFO)
    names = await tool_names(load_server(DISABLED_TAGS="delete, scripts"))

    assert "netbox_delete_object" not in names
    assert "netbox_run_script" not in names
    assert "netbox_create_object" in names
    assert "Disabled tags configured" in caplog.text


@pytest.mark.parametrize(
    ("strategy", "transform"),
    [("bm25", BM25SearchTransform), ("regex", RegexSearchTransform)],
)
async def test_tool_search_replaces_the_tool_list(
    load_server, caplog, strategy, transform
):
    caplog.set_level(logging.INFO)
    server = load_server(
        TOOL_SEARCH_ENABLED="true",
        TOOL_SEARCH_STRATEGY=strategy,
        TOOL_SEARCH_MAX_RESULTS="7",
    )

    [search] = [t for t in server.mcp.transforms if isinstance(t, transform)]
    assert search._max_results == 7
    assert await tool_names(server) == {"search_tools", "call_tool"}
    assert f"strategy={strategy}, max_results=7" in caplog.text


def test_response_size_limit_adds_middleware(load_server):
    server = load_server(RESPONSE_MAX_SIZE="4096")

    [limit] = [
        m for m in server.mcp.middleware if isinstance(m, ResponseLimitingMiddleware)
    ]
    assert limit.max_size == 4096


def test_rate_limiting_adds_middleware(load_server):
    server = load_server(
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_MAX_REQUESTS="5",
        RATE_LIMIT_WINDOW_MINUTES="2",
    )

    [limiter] = [
        m
        for m in server.mcp.middleware
        if isinstance(m, SlidingWindowRateLimitingMiddleware)
    ]
    assert limiter.max_requests == 5
    assert limiter.window_seconds == 120


def test_optional_middleware_is_off_by_default(load_server):
    server = load_server()

    assert not any(
        isinstance(m, SlidingWindowRateLimitingMiddleware | ResponseLimitingMiddleware)
        for m in server.mcp.middleware
    )


def test_instructions_name_no_mode_by_default(load_server):
    instructions = load_server().mcp.instructions

    assert "netbox_get_objects" in instructions
    assert "READ-ONLY" not in instructions
    assert "DRY-RUN" not in instructions
    assert "instead of main" not in instructions


def test_instructions_announce_read_only_mode(load_server):
    # Read-only wins over dry-run: there is nothing left to simulate.
    instructions = load_server(
        READ_ONLY_MODE="true", DRY_RUN_MODE="true"
    ).mcp.instructions

    assert "READ-ONLY mode" in instructions
    assert "DRY-RUN" not in instructions


def test_instructions_announce_dry_run_mode(load_server):
    instructions = load_server(DRY_RUN_MODE="true").mcp.instructions

    assert "DRY-RUN mode" in instructions
    assert "READ-ONLY" not in instructions


def test_instructions_name_the_default_branch(load_server):
    instructions = load_server(
        NETBOX_BRANCHING_ENABLED="true", NETBOX_BRANCH="staging"
    ).mcp.instructions

    assert "netbox-branching branch 'staging' instead of main" in instructions


def test_unknown_log_level_falls_back_to_info(load_server, caplog):
    load_server(LOG_LEVEL="chatty")

    assert "Unknown LOG_LEVEL 'CHATTY'" in caplog.text


def test_version_falls_back_when_package_is_not_installed(load_server, monkeypatch):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", missing)

    server = load_server()

    assert server.__version__ == "0.0.1"
    assert server.mcp.version == "0.0.1"


def test_invalid_configuration_is_logged_and_raised(load_server, caplog):
    with pytest.raises(ValueError, match="TOOL_SEARCH_STRATEGY"):
        load_server(TOOL_SEARCH_ENABLED="true", TOOL_SEARCH_STRATEGY="regx")

    assert "Invalid configuration" in caplog.text


def test_token_passthrough_over_stdio_is_refused_at_import(load_server, caplog):
    with pytest.raises(ValueError, match="requires MCP_TRANSPORT=http or sse"):
        load_server(NETBOX_TOKEN_PASSTHROUGH="true")

    assert "Invalid configuration" in caplog.text


def test_bearer_token_enables_static_auth(load_server):
    assert load_server().mcp.auth is None
    assert isinstance(
        load_server(MCP_HTTP_BEARER_TOKEN="secret").mcp.auth, StaticTokenVerifier
    )


def test_token_passthrough_enables_netbox_auth(load_server):
    server = load_server(MCP_TRANSPORT="http", NETBOX_TOKEN_PASSTHROUGH="true")

    assert isinstance(server.mcp.auth, NetBoxTokenVerifier)


def test_main_runs_stdio_by_default(load_server, caplog):
    caplog.set_level(logging.INFO)
    server = load_server()

    server.main()

    server.mcp.run.assert_called_once_with()
    assert "Using STDIO transport" in caplog.text
    assert "connecting to https://netbox.test (modes: read-write)" in caplog.text
    # stdio has no HTTP endpoint to protect.
    assert "WITHOUT authentication" not in caplog.text


def test_main_logs_the_active_modes(load_server, caplog):
    caplog.set_level(logging.INFO)
    server = load_server(READ_ONLY_MODE="true", DRY_RUN_MODE="true")

    server.main()

    assert "(modes: read-only, dry-run)" in caplog.text


@pytest.mark.parametrize(
    ("transport", "expected", "label"),
    [
        ("http", "http", "Streamable"),
        ("streamable-http", "http", "Streamable"),
        ("sse", "sse", "SSE"),
    ],
)
def test_main_runs_http_transports_on_the_configured_address(
    load_server, caplog, transport, expected, label
):
    caplog.set_level(logging.INFO)
    server = load_server(
        MCP_TRANSPORT=transport,
        MCP_HTTP_HOST="192.0.2.10",
        MCP_HTTP_PORT="9001",
        MCP_HTTP_BEARER_TOKEN="secret",
    )

    server.main()

    server.mcp.run.assert_called_once_with(
        transport=expected, host="192.0.2.10", port=9001
    )
    assert f"Using HTTP {label} transport on 192.0.2.10:9001" in caplog.text
    assert f"Bearer token authentication enabled for {label} transport" in caplog.text
    assert "WITHOUT authentication" not in caplog.text


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_main_warns_when_http_runs_without_a_token(load_server, caplog, transport):
    caplog.set_level(logging.INFO)
    server = load_server(MCP_TRANSPORT=transport)

    server.main()

    assert "WITHOUT authentication" in caplog.text
    assert "authentication enabled" not in caplog.text
    server.mcp.run.assert_called_once()


def test_main_with_token_passthrough_needs_no_server_token(load_server, caplog):
    caplog.set_level(logging.INFO)
    server = load_server(
        MCP_TRANSPORT="http", NETBOX_TOKEN_PASSTHROUGH="true", NETBOX_TOKEN=""
    )

    server.main()

    server.mcp.run.assert_called_once_with(
        transport="http", host="127.0.0.1", port=8000
    )
    assert "Per-user NetBox token authentication enabled" in caplog.text
    # The per-user token check is the authentication.
    assert "WITHOUT authentication" not in caplog.text
    assert "NETBOX_TOKEN is ignored" not in caplog.text


def test_main_warns_that_passthrough_ignores_the_server_token(load_server, caplog):
    server = load_server(MCP_TRANSPORT="sse", NETBOX_TOKEN_PASSTHROUGH="true")

    server.main()

    assert "NETBOX_TOKEN is ignored" in caplog.text
    server.mcp.run.assert_called_once()


def test_main_notes_a_legacy_v1_token(load_server, caplog):
    caplog.set_level(logging.INFO)
    server = load_server(NETBOX_TOKEN="0123456789abcdef")

    server.main()

    assert "legacy v1 token" in caplog.text
    server.mcp.run.assert_called_once_with()


def test_main_accepts_a_v2_token_silently(load_server, caplog):
    caplog.set_level(logging.INFO)
    server = load_server()

    server.main()

    assert "legacy v1 token" not in caplog.text


def test_main_warns_about_plaintext_http(load_server, caplog):
    server = load_server(NETBOX_URL="http://netbox.test")

    server.main()

    assert "NETBOX_URL uses plaintext http://" in caplog.text
    server.mcp.run.assert_called_once_with()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"MCP_TRANSPORT": "websocket"}, "Unknown MCP_TRANSPORT 'websocket'"),
        ({"NETBOX_URL": ""}, "Missing required NetBox URL (NETBOX_URL)"),
        (
            {"NETBOX_URL": "netbox.test"},
            "NETBOX_URL must start with https:// or http:// (got 'netbox.test')",
        ),
        (
            {"NETBOX_URL": "ftp://netbox.test"},
            "NETBOX_URL must start with https:// or http://",
        ),
        ({"NETBOX_TOKEN": ""}, "Missing NetBox API token (NETBOX_TOKEN)"),
    ],
)
def test_main_refuses_to_start(load_server, caplog, env, message):
    server = load_server(**env)

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code == 1
    assert message in caplog.text
    server.mcp.run.assert_not_called()
