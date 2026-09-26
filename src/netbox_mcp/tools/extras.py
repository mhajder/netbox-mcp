"""
Specialised read tools: status, GraphQL, IPAM availability, cable trace, config.
"""

import re
from typing import Annotated
from typing import Any
from typing import Literal

import httpx2
from fastmcp import Context
from pydantic import Field

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import NetBoxAPIError
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.object_types import endpoint_for
from netbox_mcp.tools.common import READ_ANNOTATIONS
from netbox_mcp.tools.common import READ_ONLY_TAG
from netbox_mcp.tools.common import BranchArg
from netbox_mcp.tools.common import ObjectIdArg
from netbox_mcp.tools.common import netbox_tool
from netbox_mcp.tools.errors import fail

# Parent type -> the kinds of free resources NetBox can list for it.
AVAILABLE_KINDS: dict[str, tuple[str, ...]] = {
    "ipam.prefix": ("ips", "prefixes"),
    "ipam.iprange": ("ips",),
    "ipam.vlangroup": ("vlans",),
    "ipam.asnrange": ("asns",),
}

AvailableParentArg = Annotated[
    Literal["ipam.prefix", "ipam.iprange", "ipam.vlangroup", "ipam.asnrange"],
    Field(description="Type of the parent object that holds the pool."),
]
AvailableKindArg = Annotated[
    Literal["ips", "prefixes", "vlans", "asns"],
    Field(
        description="What to look for: 'ips' (prefix or IP range), 'prefixes' "
        "(prefix), 'vlans' (VLAN group), 'asns' (ASN range)."
    ),
]

# Path endpoints (a cable path starts or ends here) expose /trace/; pass-through
# ports expose /paths/, listing every cable path that runs through them.
PATH_ENDPOINT_TYPES = (
    "dcim.interface",
    "dcim.consoleport",
    "dcim.consoleserverport",
    "dcim.powerport",
    "dcim.poweroutlet",
    "dcim.powerfeed",
)
PASS_THROUGH_TYPES = (
    "dcim.frontport",
    "dcim.rearport",
    "circuits.circuittermination",
    "circuits.virtualcircuittermination",
)
TRACEABLE_TYPES = Literal[
    "dcim.interface",
    "dcim.consoleport",
    "dcim.consoleserverport",
    "dcim.powerport",
    "dcim.poweroutlet",
    "dcim.powerfeed",
    "dcim.frontport",
    "dcim.rearport",
    "circuits.circuittermination",
    "circuits.virtualcircuittermination",
]

_GRAPHQL_STRINGS = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|#[^\n]*')
_GRAPHQL_WRITE_OPERATION = re.compile(r"(?:^|[\s}])(mutation|subscription)\b")


def available_path(parent_type: str, parent_id: int, kind: str) -> str:
    """Build the ``available-*`` endpoint path for a parent object.

    Raises:
        ValueError: If the parent type has no pool of that kind.
    """
    if kind not in AVAILABLE_KINDS[parent_type]:
        raise ValueError(
            f"{parent_type} has no available {kind}. Valid kinds: "
            + ", ".join(AVAILABLE_KINDS[parent_type])
        )
    endpoint = endpoint_for(parent_type)
    return f"{endpoint}/{parent_id}/available-{kind}"


def _node(obj: Any) -> Any:
    """Summarize one cable-path endpoint: what it is and which device it is on."""
    if not isinstance(obj, dict):
        return obj
    node = {key: obj[key] for key in ("id", "display", "name", "label") if key in obj}
    for parent in ("device", "circuit", "power_panel", "module"):
        value = obj.get(parent)
        if isinstance(value, dict):
            node[parent] = value.get("display") or value.get("name")
    url = obj.get("url")
    if isinstance(url, str) and "/api/" in url:
        # "/api/dcim/interfaces/7/" -> "dcim/interfaces"
        node["endpoint"] = "/".join(url.split("/api/", 1)[1].strip("/").split("/")[:2])
    return node


def register_extras_tools(
    mcp, config: NetBoxConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register specialised read tools with the MCP server"""

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "status", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_get_status(ctx: Context) -> dict:
        """Get NetBox version, installed plugins and apps, worker status and the
        authenticated user. Also reports this server's read-only/dry-run modes.
        """
        try:
            async with NetBoxClient(config, transport=transport) as nb:
                status = await nb.get("status", use_branch=False)
                user = None
                try:
                    auth = await nb.get("authentication-check", use_branch=False)
                    user = {k: auth.get(k) for k in ("id", "username", "is_active")}
                except NetBoxAPIError as exc:
                    if exc.status != 404:  # endpoint added in NetBox 4.5
                        raise
            return {
                "netbox": status,
                "user": user,
                "server": {
                    "read_only_mode": config.read_only_mode,
                    "dry_run_mode": config.dry_run_mode,
                    "branching_enabled": config.branching_enabled,
                    "default_branch": config.default_branch,
                    "plugin_discovery": config.plugin_discovery,
                },
            }
        except Exception as e:
            await fail(ctx, "Error retrieving NetBox status", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "graphql", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_graphql_query(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                min_length=1,
                description="GraphQL query. Fetch related data in one round trip, "
                'e.g. \'{ device_list(filters: {site: {name: {exact: "DC1"}}}) '
                "{ id name interfaces { name ip_addresses { address } } } }'. "
                "NetBox 4.5+ requires lookups on filters ({exact: ...}).",
            ),
        ],
        variables: Annotated[
            dict[str, Any] | None,
            Field(default=None, description="GraphQL variables."),
        ] = None,
        branch: BranchArg = None,
    ) -> dict:
        """Run a read-only GraphQL query against NetBox's /graphql/ endpoint.

        Use it to fetch nested data that would otherwise need many REST calls.
        Mutations and subscriptions are rejected. Always paginate large lists,
        e.g. device_list(pagination: {limit: 50}).
        """
        try:
            stripped = _GRAPHQL_STRINGS.sub(" ", query)
            if _GRAPHQL_WRITE_OPERATION.search(stripped):
                raise ValueError("Only GraphQL queries are allowed, not mutations.")
            payload: dict[str, Any] = {"query": query}
            if variables:
                payload["variables"] = variables
            async with NetBoxClient(config, branch, transport=transport) as nb:
                result = await nb.post("graphql", payload, absolute=True)
            if result.get("errors") and not result.get("data"):
                raise ValueError(f"GraphQL errors: {result['errors']}")
            return result
        except Exception as e:
            await fail(ctx, "Error running GraphQL query", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "ipam", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_get_available(
        ctx: Context,
        parent_type: AvailableParentArg,
        parent_id: ObjectIdArg,
        kind: AvailableKindArg,
        limit: Annotated[
            int,
            Field(default=10, ge=1, le=1000, description="Maximum results."),
        ] = 10,
        branch: BranchArg = None,
    ) -> dict:
        """List free IPs, child prefixes, VLAN IDs or ASNs in a pool.

        Examples: free IPs in prefix 12 -> ('ipam.prefix', 12, 'ips'); free /24s
        in a prefix -> ('ipam.prefix', 12, 'prefixes'); free VLAN IDs in group 3
        -> ('ipam.vlangroup', 3, 'vlans').
        """
        try:
            path = available_path(parent_type, parent_id, kind)
            async with NetBoxClient(config, branch, transport=transport) as nb:
                results = await nb.get(path, params={"limit": limit})
            results = results if isinstance(results, list) else [results]
            return {"results": results[:limit], "count": len(results[:limit])}
        except Exception as e:
            await fail(ctx, f"Error listing available {kind}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "dcim", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_trace_cable(
        ctx: Context,
        object_type: Annotated[
            TRACEABLE_TYPES, Field(description="Type of the starting port.")
        ],
        object_id: ObjectIdArg,
        branch: BranchArg = None,
    ) -> dict:
        """Trace cable paths from a port through patch panels and circuits.

        For an interface, console, power port/outlet or power feed: the path
        from it to the far end, one hop (near end, cable, far end) per segment.
        For a pass-through port (front/rear port, circuit termination): every
        cable path running through it, each as its list of nodes.
        """
        try:
            endpoint = endpoint_for(object_type)
            async with NetBoxClient(config, branch, transport=transport) as nb:
                if object_type in PASS_THROUGH_TYPES:
                    paths = await nb.get(f"{endpoint}/{object_id}/paths")
                else:
                    segments = await nb.get(f"{endpoint}/{object_id}/trace")
            if object_type in PASS_THROUGH_TYPES:
                return {
                    "paths": [
                        {
                            "id": path.get("id"),
                            "is_active": path.get("is_active"),
                            "is_complete": path.get("is_complete"),
                            "is_split": path.get("is_split"),
                            "nodes": [
                                [_node(n) for n in step]
                                for step in path.get("path", [])
                            ],
                        }
                        for path in paths or []
                    ],
                    "count": len(paths or []),
                }
            hops = []
            for segment in segments or []:
                near, cable, far = [*segment, None, None, None][:3]
                hops.append(
                    {
                        "near_end": [_node(n) for n in near or []],
                        "cable": _node(cable) if cable else None,
                        "far_end": [_node(n) for n in far or []],
                    }
                )
            return {
                "hops": hops,
                "count": len(hops),
                "complete": bool(hops and hops[-1]["far_end"]),
            }
        except Exception as e:
            await fail(ctx, f"Error tracing {object_type} {object_id}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "config", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_render_config(
        ctx: Context,
        object_type: Annotated[
            Literal["dcim.device", "virtualization.virtualmachine"],
            Field(description="Device or virtual machine."),
        ],
        object_id: ObjectIdArg,
        extra_context: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Extra variables merged into the template context.",
            ),
        ] = None,
        config_template_id: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                description="Render this config template instead of the one "
                "assigned to the object.",
            ),
        ] = None,
        branch: BranchArg = None,
    ) -> dict:
        """Render the configuration of a device or VM from its assigned config
        template (NetBox config templates + config context). Changes nothing,
        but NetBox only allows it for API tokens with write enabled.
        """
        try:
            endpoint = endpoint_for(object_type)
            async with NetBoxClient(config, branch, transport=transport) as nb:
                payload = dict(extra_context or {})
                if config_template_id:
                    payload["config_template_id"] = config_template_id
                result = await nb.post(f"{endpoint}/{object_id}/render-config", payload)
            return result if isinstance(result, dict) else {"configuration": result}
        except Exception as e:
            await fail(ctx, f"Error rendering config for {object_type} {object_id}", e)
