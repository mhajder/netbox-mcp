#!/usr/bin/env python3
"""
NetBox MCP Server

Provides a Model Context Protocol (MCP) server exposing tools that interact with the NetBox API.
"""

import logging
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp import settings
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware.rate_limiting import SlidingWindowRateLimitingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search import RegexSearchTransform

from netbox_mcp.netbox_client import get_netbox_config_from_env
from netbox_mcp.netbox_client import get_transport_config_from_env
from netbox_mcp.sentry_init import init_sentry
from netbox_mcp.tools import register_tools

# Load environment variables
load_dotenv()

# Configure FastMCP defaults
settings.show_server_banner = False
settings.check_for_updates = "off"

# Initialize optional Sentry monitoring
init_sentry()

# Configure logging. An unknown or lowercase LOG_LEVEL must not take the server
# down, so resolve it leniently and fall back to INFO.
_requested_log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
_resolved_log_level = logging.getLevelNamesMapping().get(_requested_log_level)
logging.basicConfig(
    level=_resolved_log_level if _resolved_log_level is not None else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

if _resolved_log_level is None:
    logger.warning(
        "Unknown LOG_LEVEL %r - falling back to INFO. Valid values: %s",
        _requested_log_level,
        ", ".join(logging.getLevelNamesMapping()),
    )

# Get package version
try:
    __version__ = version("netbox-mcp")
except PackageNotFoundError:
    __version__ = "0.0.1"

try:
    NETBOX_CONFIG = get_netbox_config_from_env()
    TRANSPORT_CONFIG = get_transport_config_from_env()
except Exception as e:
    logger.error(f"Invalid configuration: {e}")
    raise

# Create auth provider if bearer token is configured
auth_provider = None
if getattr(TRANSPORT_CONFIG, "http_bearer_token", None):
    bearer_token = TRANSPORT_CONFIG.http_bearer_token
    if bearer_token:  # Type narrowing: ensures bearer_token is str, not None
        auth_provider = StaticTokenVerifier(
            tokens={
                bearer_token: {
                    "client_id": "authenticated-client",
                    "scopes": ["read", "write"],
                }
            }
        )


def build_instructions() -> str:
    """Describe the server to the model, including the active safety modes."""
    parts = [
        "This MCP server exposes tools for the NetBox DCIM/IPAM API. Generic tools "
        "(netbox_get_objects, netbox_get_object, netbox_create_object, ...) work for "
        "every object type, named 'app.model' (e.g. 'dcim.device', 'ipam.prefix'). "
        "Always pass 'fields' when listing objects to keep responses small, and "
        "resolve related objects to numeric IDs before filtering or writing. Use "
        "netbox_describe_object_type before a write to learn the required fields."
    ]
    if NETBOX_CONFIG.read_only_mode:
        parts.append("The server is in READ-ONLY mode: no write tools are available.")
    elif NETBOX_CONFIG.dry_run_mode:
        parts.append(
            "The server is in DRY-RUN mode: write tools only return the request "
            "they would send and never change NetBox. Tell the user that changes "
            "were simulated."
        )
    if NETBOX_CONFIG.default_branch:
        parts.append(
            f"Calls default to the netbox-branching branch "
            f"{NETBOX_CONFIG.default_branch!r} instead of main."
        )
    return " ".join(parts)


# Initialize FastMCP server
mcp = FastMCP(
    name="NetBox MCP Server",
    version=__version__,
    instructions=build_instructions(),
    auth=auth_provider,
)

# Register all tools
register_tools(mcp, NETBOX_CONFIG)


def configure_component_visibility() -> None:
    """Apply server-level visibility transforms for read-only and disabled tags."""

    disabled_tags = getattr(NETBOX_CONFIG, "disabled_tags", set())
    read_only_mode = getattr(NETBOX_CONFIG, "read_only_mode", False)

    if read_only_mode:
        logger.info("Read-only mode is enabled - restricting to read-only components")
        mcp.enable(tags={"read-only"}, only=True)

    if disabled_tags:
        logger.info(
            "Disabled tags configured: %s - disabling matching components",
            disabled_tags,
        )
        mcp.disable(tags=disabled_tags)


def configure_tool_search() -> None:
    """Apply the optional FastMCP tool-search transform."""

    if not getattr(NETBOX_CONFIG, "tool_search_enabled", False):
        return

    strategy = getattr(NETBOX_CONFIG, "tool_search_strategy", "bm25")
    max_results = getattr(NETBOX_CONFIG, "tool_search_max_results", 5)

    if strategy == "regex":
        mcp.add_transform(RegexSearchTransform(max_results=max_results))
    else:
        mcp.add_transform(BM25SearchTransform(max_results=max_results))

    logger.info(
        "Tool search is enabled - strategy=%s, max_results=%s",
        strategy,
        max_results,
    )


configure_component_visibility()
configure_tool_search()

# Optional response size limit
if NETBOX_CONFIG.response_max_size:
    logger.info(
        "Response size limit is enabled - max %d bytes", NETBOX_CONFIG.response_max_size
    )
    mcp.add_middleware(
        ResponseLimitingMiddleware(max_size=NETBOX_CONFIG.response_max_size)
    )

# Optional rate limiting
if getattr(NETBOX_CONFIG, "rate_limit_enabled", False):
    logger.info("Rate limiting is enabled - applying middleware")
    mcp.add_middleware(
        SlidingWindowRateLimitingMiddleware(
            max_requests=NETBOX_CONFIG.rate_limit_max_requests,
            window_minutes=NETBOX_CONFIG.rate_limit_window_minutes,
        )
    )


def main():
    if not NETBOX_CONFIG.netbox_url:
        logger.error("Missing required NetBox URL (NETBOX_URL). Check your .env file.")
        raise SystemExit(1)

    # Without a scheme the request URL is ambiguous, and a silent fallback to
    # http:// would put the API token on the wire in cleartext.
    scheme = urlparse(NETBOX_CONFIG.netbox_url).scheme.lower()
    if scheme not in {"http", "https"}:
        logger.error(
            "NETBOX_URL must start with https:// or http:// (got %r).",
            NETBOX_CONFIG.netbox_url,
        )
        raise SystemExit(1)
    if scheme == "http":
        logger.warning(
            "NETBOX_URL uses plaintext http://. The API token is sent in the "
            "Authorization header on every request - use https:// outside a trusted network."
        )

    if not NETBOX_CONFIG.token:
        logger.error("Missing NetBox API token (NETBOX_TOKEN). Check your .env file.")
        raise SystemExit(1)
    if not NETBOX_CONFIG.token.startswith("nbt_"):
        logger.info(
            "NETBOX_TOKEN is a legacy v1 token. v1 tokens are deprecated since "
            "NetBox 4.6 - consider a v2 token (nbt_...)."
        )

    if (
        TRANSPORT_CONFIG.transport_type in {"sse", "http"}
        and not TRANSPORT_CONFIG.http_bearer_token
    ):
        logger.warning(
            "WARNING: MCP_HTTP_BEARER_TOKEN is not set. The MCP server will run WITHOUT authentication. "
            "Ensure the server is not exposed to untrusted networks (e.g. bind to 127.0.0.1 instead of 0.0.0.0)."
        )

    modes = [
        name
        for name, enabled in (
            ("read-only", NETBOX_CONFIG.read_only_mode),
            ("dry-run", NETBOX_CONFIG.dry_run_mode),
        )
        if enabled
    ]
    logger.info(
        "Starting NetBox MCP Server connecting to %s (modes: %s)...",
        NETBOX_CONFIG.netbox_url,
        ", ".join(modes) or "read-write",
    )

    # Choose transport based on configuration
    if TRANSPORT_CONFIG.transport_type == "sse":
        logger.info(
            f"Using HTTP SSE transport on {TRANSPORT_CONFIG.http_host}:{TRANSPORT_CONFIG.http_port}"
        )
        if TRANSPORT_CONFIG.http_bearer_token:
            logger.info("Bearer token authentication enabled for SSE transport")

        # Run with HTTP SSE transport
        mcp.run(
            transport="sse",
            host=TRANSPORT_CONFIG.http_host,
            port=TRANSPORT_CONFIG.http_port,
        )
    elif TRANSPORT_CONFIG.transport_type == "http":
        logger.info(
            f"Using HTTP Streamable transport on {TRANSPORT_CONFIG.http_host}:{TRANSPORT_CONFIG.http_port}"
        )
        if TRANSPORT_CONFIG.http_bearer_token:
            logger.info("Bearer token authentication enabled for Streamable transport")

        # Run with HTTP Streamable transport
        mcp.run(
            transport="http",
            host=TRANSPORT_CONFIG.http_host,
            port=TRANSPORT_CONFIG.http_port,
        )
    else:
        # Default to STDIO transport
        logger.info("Using STDIO transport")
        mcp.run()


if __name__ == "__main__":
    main()
