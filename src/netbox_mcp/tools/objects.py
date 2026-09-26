"""
Generic read tools that work for every NetBox object type.
"""

import asyncio
from typing import Annotated
from typing import Any

import httpx2
from fastmcp import Context
from pydantic import Field

from netbox_mcp.filters import encode_params
from netbox_mcp.filters import validate_filters
from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import NetBoxAPIError
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.object_types import all_object_types
from netbox_mcp.object_types import plugin_object_types
from netbox_mcp.tools.common import READ_ANNOTATIONS
from netbox_mcp.tools.common import READ_ONLY_TAG
from netbox_mcp.tools.common import BranchArg
from netbox_mcp.tools.common import BriefArg
from netbox_mcp.tools.common import FieldsArg
from netbox_mcp.tools.common import ObjectIdArg
from netbox_mcp.tools.common import ObjectTypeArg
from netbox_mcp.tools.common import OmitArg
from netbox_mcp.tools.common import netbox_tool
from netbox_mcp.tools.common import page
from netbox_mcp.tools.common import pick
from netbox_mcp.tools.common import project
from netbox_mcp.tools.common import shape_params
from netbox_mcp.tools.errors import fail

DEFAULT_SEARCH_TYPES = [
    "dcim.device",
    "dcim.site",
    "ipam.ipaddress",
    "ipam.prefix",
    "dcim.interface",
    "dcim.rack",
    "ipam.vlan",
    "circuits.circuit",
    "virtualization.virtualmachine",
]

# Change log fields returned unless the caller asks for the object snapshots,
# which are several KB per entry.
CHANGELOG_SUMMARY_FIELDS = (
    "id,time,user_name,request_id,action,changed_object_type,changed_object_id,"
    "object_repr,message"
)

# Choice lists longer than this are cut in describe output (e.g. interface types).
MAX_CHOICES = 60

FiltersArg = Annotated[
    dict[str, Any] | None,
    Field(
        default=None,
        description="NetBox filters, e.g. {'site_id': 1, 'status': 'active', "
        "'name__ic': 'core'}. Pass a list for OR: {'id': [1, 2]}. null matches "
        "empty: {'tenant_id': None}. Lookup suffixes: n, ic, nic, isw, nisw, iew, "
        "niew, ie, nie, empty, regex, iregex, lt, lte, gt, gte. NOT supported: "
        "'__in' and multi-hop lookups like 'device__site_id' - query the related "
        "object first, then filter by its ID.",
    ),
]
LimitArg = Annotated[
    int,
    Field(default=10, ge=1, le=1000, description="Page size (1-1000)."),
]
OffsetArg = Annotated[
    int, Field(default=0, ge=0, description="Number of results to skip.")
]


def register_objects_tools(
    mcp, config: NetBoxConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register generic object read tools with the MCP server"""

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "objects", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_list_object_types(
        ctx: Context,
        app: Annotated[
            str | None,
            Field(
                default=None,
                description="Only types of this app, e.g. 'dcim', 'ipam', 'netbox_dns'.",
            ),
        ] = None,
        rediscover: Annotated[
            bool,
            Field(
                default=False,
                description="Re-read plugin types from NetBox (plugin discovery must "
                "be enabled on the server).",
            ),
        ] = False,
    ) -> dict:
        """List the NetBox object types these tools accept, with their API endpoints.

        Types are named 'app.model' (e.g. 'dcim.device'). Plugin types appear when
        the server runs with plugin discovery enabled.
        """
        try:
            if config.plugin_discovery:
                async with NetBoxClient(config, transport=transport) as nb:
                    await nb.discover_plugin_types(force=rediscover)
            types = all_object_types()
            if app:
                types = {k: v for k, v in types.items() if k.split(".")[0] == app}
            return {
                "count": len(types),
                "object_types": dict(sorted(types.items())),
                "plugin_types": sorted(set(types) & set(plugin_object_types())),
            }
        except Exception as e:
            await fail(ctx, "Error listing object types", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "objects", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_get_objects(
        ctx: Context,
        object_type: ObjectTypeArg,
        filters: FiltersArg = None,
        fields: FieldsArg = None,
        omit: OmitArg = None,
        brief: BriefArg = False,
        limit: LimitArg = 10,
        offset: OffsetArg = 0,
        ordering: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description="Sort field(s); prefix with '-' for descending, e.g. "
                "'name' or ['site', '-name'].",
            ),
        ] = None,
        branch: BranchArg = None,
    ) -> dict:
        """List NetBox objects of one type, with filtering and pagination.

        Returns 'results' plus 'total' (all matches) and 'has_more'/'next_offset'.
        Results are paginated - check 'has_more' before concluding. To count
        objects cheaply use fields=['id'] and limit=1 and read 'total'.

        Examples:
            netbox_get_objects('dcim.device', {'site_id': 3, 'status': 'active'},
                               fields=['id', 'name', 'primary_ip4'])
            netbox_get_objects('ipam.ipaddress', {'parent': '10.0.0.0/24'},
                               fields=['address', 'dns_name'])
        """
        try:
            filters = filters or {}
            validate_filters(filters)
            if isinstance(ordering, list):
                ordering = ",".join(ordering)
            await ctx.info(f"Retrieving {object_type} objects...")
            async with NetBoxClient(config, branch, transport=transport) as nb:
                key, endpoint, fallback = await nb.resolve(object_type)
                params = encode_params(filters, drop_none=False) + encode_params(
                    {
                        "limit": limit,
                        "offset": offset,
                        "ordering": ordering or None,
                        **await shape_params(nb, fields, omit, brief),
                    }
                )
                data = await nb.get(endpoint, params=params, fallback_path=fallback)
                data = data or {}
                data["results"] = project(data.get("results", []), fields)
                return page(key, data, limit, offset)
        except Exception as e:
            await fail(ctx, f"Error retrieving {object_type} objects", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "objects", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_get_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        object_id: ObjectIdArg,
        fields: FieldsArg = None,
        omit: OmitArg = None,
        brief: BriefArg = False,
        branch: BranchArg = None,
    ) -> dict:
        """Get one NetBox object by type and ID."""
        try:
            async with NetBoxClient(config, branch, transport=transport) as nb:
                _, endpoint, fallback = await nb.resolve(object_type)
                obj = await nb.get(
                    f"{endpoint}/{object_id}",
                    params=await shape_params(nb, fields, omit, brief),
                    fallback_path=f"{fallback}/{object_id}" if fallback else None,
                )
                return pick(obj, fields) if fields else obj
        except Exception as e:
            await fail(ctx, f"Error retrieving {object_type} {object_id}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "search", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_search_objects(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                min_length=1,
                description="Free text: a name, IP, serial, asset tag, description...",
            ),
        ],
        object_types: Annotated[
            list[str] | None,
            Field(
                default=None,
                description="Types to search. Default: "
                + ", ".join(DEFAULT_SEARCH_TYPES),
            ),
        ] = None,
        fields: FieldsArg = None,
        limit: Annotated[
            int,
            Field(default=5, ge=1, le=100, description="Maximum results per type."),
        ] = 5,
        branch: BranchArg = None,
    ) -> dict:
        """Search several NetBox object types at once with NetBox's 'q' filter.

        Returns results keyed by object type. A '_meta' key appears when some
        types failed ('errors' - treat as unknown, NOT as "no match") or had more
        matches than 'limit' ('truncated' - the full count per type).
        """
        try:
            search_types = object_types or DEFAULT_SEARCH_TYPES
            async with NetBoxClient(config, branch, transport=transport) as nb:
                resolved = [await nb.resolve(t) for t in search_types]

                async def search_one(endpoint: str, fallback: str | None) -> Any:
                    return await nb.get(
                        endpoint,
                        params={
                            "q": query,
                            "limit": limit,
                            "fields": ",".join(fields) if fields else None,
                        },
                        fallback_path=fallback,
                    )

                responses = await asyncio.gather(
                    *(
                        search_one(endpoint, fallback)
                        for _, endpoint, fallback in resolved
                    ),
                    return_exceptions=True,
                )

            results: dict[str, Any] = {}
            errors: dict[str, str] = {}
            truncated: dict[str, int] = {}
            for (key, _, _), response in zip(resolved, responses, strict=True):
                if isinstance(response, NetBoxAPIError):
                    # Auth failures and a failing NetBox affect every type; hiding
                    # them per type would read as "not found".
                    if response.status in (401, 403) or response.status >= 500:
                        raise response
                    errors[key] = f"HTTP {response.status}: {response.body}"
                    results[key] = []
                    continue
                if isinstance(response, BaseException):
                    raise response
                response = response or {}
                rows = response.get("results", [])
                results[key] = project(rows, fields)
                count = response.get("count")
                if isinstance(count, int) and count > len(rows):
                    truncated[key] = count

            meta: dict[str, Any] = {}
            if errors:
                meta["errors"] = errors
            if truncated:
                meta["truncated"] = truncated
            if meta:
                results["_meta"] = meta
            return results
        except Exception as e:
            await fail(ctx, f"Error searching for {query!r}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "objects", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_describe_object_type(
        ctx: Context,
        object_type: ObjectTypeArg,
    ) -> dict:
        """Describe the writable fields of an object type: type, required, choices.

        Call this before creating or updating an object to build a valid payload.
        Foreign keys take the related object's numeric ID.
        """
        try:
            async with NetBoxClient(config, transport=transport) as nb:
                key, endpoint, fallback = await nb.resolve(object_type)
                data = await nb.options(endpoint, fallback_path=fallback)
            post = (data or {}).get("actions", {}).get("POST")
            if not post:
                return {
                    "object_type": key,
                    "endpoint": f"/api/{endpoint}/",
                    "fields": {},
                    "note": "NetBox returned no field schema - the token probably "
                    "lacks 'add' permission for this type, or it is read-only.",
                }
            fields: dict[str, Any] = {}
            for name, spec in post.items():
                if spec.get("read_only"):
                    continue
                field: dict[str, Any] = {"type": spec.get("type")}
                if spec.get("required"):
                    field["required"] = True
                if spec.get("help_text"):
                    field["help"] = spec["help_text"]
                choices = [c.get("value") for c in spec.get("choices", [])]
                if choices:
                    field["choices"] = choices[:MAX_CHOICES]
                    if len(choices) > MAX_CHOICES:
                        field["choices_truncated"] = len(choices)
                fields[name] = field
            return {
                "object_type": key,
                "name": data.get("name"),
                "endpoint": f"/api/{endpoint}/",
                "required": sorted(n for n, f in fields.items() if f.get("required")),
                "fields": fields,
            }
        except Exception as e:
            await fail(ctx, f"Error describing {object_type}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "changelog", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_get_changelogs(
        ctx: Context,
        filters: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Change log filters, e.g. {'changed_object_type': "
                "'dcim.device', 'changed_object_id': 5}, {'user': 'admin'}, "
                "{'action': 'delete'}, {'time_after': '2026-01-01T00:00:00Z'}, "
                "{'request_id': '<uuid>'}.",
            ),
        ] = None,
        limit: LimitArg = 10,
        offset: OffsetArg = 0,
        include_data: Annotated[
            bool,
            Field(
                default=False,
                description="Include prechange_data/postchange_data - the full "
                "object before and after the change (several KB per entry). Use "
                "it with a narrow filter, e.g. one changed_object_id.",
            ),
        ] = False,
        branch: BranchArg = None,
    ) -> dict:
        """List change log entries (who changed what and when), newest first.

        Each entry has action (create/update/delete), user_name, time,
        changed_object_type/id and object_repr. Entries of one request share a
        request_id. Set include_data to see what exactly changed.
        """
        try:
            filters = filters or {}
            validate_filters(filters)
            async with NetBoxClient(config, branch, transport=transport) as nb:
                params = encode_params(filters, drop_none=False) + encode_params(
                    {
                        "limit": limit,
                        "offset": offset,
                        "ordering": "-time",
                        "fields": None if include_data else CHANGELOG_SUMMARY_FIELDS,
                    }
                )
                data = await nb.get(
                    "core/object-changes",
                    params=params,
                    fallback_path="extras/object-changes",  # NetBox < 4.1
                )
                return page("core.objectchange", data, limit, offset)
        except Exception as e:
            await fail(ctx, "Error retrieving change log", e)
