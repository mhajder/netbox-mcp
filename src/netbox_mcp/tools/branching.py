"""
netbox-branching tools: list branches and create new ones.

Every other tool takes a ``branch`` argument that routes it into a branch. Merge,
sync and revert are deliberately not exposed: they change the main schema in
bulk and belong in a human review step.
"""

from typing import Annotated
from typing import Any

import httpx2
from fastmcp import Context
from pydantic import Field

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import BRANCH_FIELDS
from netbox_mcp.netbox_client import BRANCHES_ENDPOINT
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.tools.common import CREATE_ANNOTATIONS
from netbox_mcp.tools.common import READ_ANNOTATIONS
from netbox_mcp.tools.common import READ_ONLY_TAG
from netbox_mcp.tools.common import dry_run_result
from netbox_mcp.tools.common import netbox_tool
from netbox_mcp.tools.common import page
from netbox_mcp.tools.common import pick
from netbox_mcp.tools.errors import fail


def _branch_row(branch: Any) -> Any:
    """Trim a branch to BRANCH_FIELDS, reducing nested objects (owner) to
    ``{id, display}``. Choice fields such as status ``{value, label}`` stay."""
    row = pick(branch, BRANCH_FIELDS)
    if not isinstance(row, dict):
        return row
    return {
        key: pick(value, ("id", "display"))
        if isinstance(value, dict) and "id" in value
        else value
        for key, value in row.items()
    }


def register_branching_tools(
    mcp, config: NetBoxConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register netbox-branching tools with the MCP server"""

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "branching", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_list_branches(
        ctx: Context,
        status: Annotated[
            str | None,
            Field(
                default=None,
                description="Filter by status, e.g. 'ready', 'provisioning', "
                "'merged', 'archived'.",
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(default=None, description="Filter by name (case-insensitive)."),
        ] = None,
        limit: Annotated[
            int, Field(default=25, ge=1, le=1000, description="Page size.")
        ] = 25,
        offset: Annotated[int, Field(default=0, ge=0, description="Offset.")] = 0,
    ) -> dict:
        """List netbox-branching branches. Pass a branch 'name' as the 'branch'
        argument of other tools to read or write inside it (status must be
        'ready').
        """
        try:
            params: dict[str, Any] = {
                # Harmless if ignored; lets a plugin version that honours it
                # send less.
                "fields": ",".join(BRANCH_FIELDS),
                "limit": limit,
                "offset": offset,
                "status": status,
                "name__ic": name,
            }
            async with NetBoxClient(config, transport=transport) as nb:
                data = await nb.get(BRANCHES_ENDPOINT, params=params, use_branch=False)
            data = data or {}
            # netbox-branching 1.2 ignores ?fields=, so trim the rows here.
            data["results"] = [_branch_row(row) for row in data.get("results", [])]
            return page("netbox_branching.branch", data, limit, offset)
        except Exception as e:
            await fail(ctx, "Error listing branches", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "branching", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def netbox_create_branch(
        ctx: Context,
        name: Annotated[str, Field(min_length=1, description="Branch name.")],
        description: Annotated[
            str | None, Field(default=None, description="What the branch is for.")
        ] = None,
    ) -> dict:
        """Create a netbox-branching branch to stage changes away from main.

        Provisioning runs in the background: poll netbox_list_branches until the
        status is 'ready' before writing to it.
        """
        try:
            payload: dict[str, Any] = {"name": name}
            if description:
                payload["description"] = description
            if config.dry_run_mode:
                return dry_run_result(
                    "create", "POST", BRANCHES_ENDPOINT, payload, None
                )
            async with NetBoxClient(config, transport=transport) as nb:
                created = await nb.post(BRANCHES_ENDPOINT, payload, use_branch=False)
            return {"success": True, "branch": _branch_row(created)}
        except Exception as e:
            await fail(ctx, f"Error creating branch {name!r}", e)
