"""
NetBox MCP Server Tools package
"""

import httpx2

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.tools.branching import register_branching_tools
from netbox_mcp.tools.extras import register_extras_tools
from netbox_mcp.tools.objects import register_objects_tools
from netbox_mcp.tools.writes import register_writes_tools


def register_tools(
    mcp, config: NetBoxConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register all NetBox tools with the MCP server.

    Args:
        mcp: The FastMCP server.
        config: NetBox connection settings.
        transport: Transport for every NetBox request. None uses the shared
            network transport; tests pass an httpx2.MockTransport.
    """
    register_objects_tools(mcp, config, transport)
    register_extras_tools(mcp, config, transport)
    if config.branching_enabled:
        register_branching_tools(mcp, config, transport)
    register_writes_tools(mcp, config, transport)
