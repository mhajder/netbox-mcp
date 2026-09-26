"""
Shared argument types, annotations and response helpers for NetBox tools.
"""

import inspect
from collections.abc import Callable
from collections.abc import Iterable
from typing import Annotated
from typing import Any

from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform
from pydantic import Field

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import NetBoxClient

READ_ONLY_TAG = "read-only"

READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
CREATE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
UPDATE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
DELETE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

# NetBox added ?omit= in 4.5.2.
OMIT_MIN_VERSION = (4, 5, 2)

ObjectTypeArg = Annotated[
    str,
    Field(
        description="NetBox object type as 'app.model' (e.g. 'dcim.device', "
        "'ipam.ipaddress', 'virtualization.virtualmachine') or as an API path "
        "('dcim/devices'). netbox_list_object_types lists every type."
    ),
]
ObjectIdArg = Annotated[int, Field(description="Numeric object ID.", ge=1)]
BranchArg = Annotated[
    str | None,
    Field(
        default=None,
        description="netbox-branching branch (name or schema ID) to work in. "
        "Omit to use the main schema or the server's default branch.",
    ),
]
FieldsArg = Annotated[
    list[str] | None,
    Field(
        default=None,
        description="Only return these fields, e.g. ['id', 'name', 'status']. "
        "ALWAYS pass this for listings - full objects cost 10x more tokens.",
    ),
]
OmitArg = Annotated[
    list[str] | None,
    Field(
        default=None,
        description="Drop these fields from the response, e.g. ['config_context', "
        "'custom_fields']. NetBox 4.5.2+. Ignored when 'fields' is set.",
    ),
]
BriefArg = Annotated[
    bool,
    Field(
        default=False,
        description="Return the minimal representation (id, url, display, name).",
    ),
]


def netbox_tool(
    mcp, config: NetBoxConfig, **kwargs: Any
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool like ``@mcp.tool(...)``, adapting it to the config.

    With branching disabled, a tool's ``branch`` argument is hidden from its
    schema: the model never sees an option the server cannot honour, and a
    call that passes it anyway is rejected.

    Args:
        mcp: The FastMCP server.
        config: NetBox settings.
        **kwargs: Passed to the tool, e.g. ``tags`` and ``annotations``.

    Returns:
        A decorator that registers the function and returns it unchanged.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool = Tool.from_function(fn, **kwargs)
        if (
            not config.branching_enabled
            and "branch" in inspect.signature(fn).parameters
        ):
            tool = Tool.from_tool(
                tool, transform_args={"branch": ArgTransform(hide=True)}
            )
        mcp.add_tool(tool)
        return fn

    return decorator


async def shape_params(
    nb: NetBoxClient,
    fields: list[str] | None,
    omit: list[str] | None,
    brief: bool,
) -> dict[str, Any]:
    """Build the response-shaping query parameters.

    Args:
        nb: Open client, used to check the NetBox version for ``omit``.
        fields: Fields to keep.
        omit: Fields to drop.
        brief: Request the brief representation.

    Returns:
        dict[str, Any]: ``fields``/``omit``/``brief`` parameters to send.

    Raises:
        ValueError: If ``omit`` is used against a NetBox older than 4.5.2, which
            would ignore it and return full objects.
    """
    params: dict[str, Any] = {}
    if fields:
        params["fields"] = ",".join(fields)
    elif omit:
        version = await nb.version()
        if version is not None and version < OMIT_MIN_VERSION:
            raise ValueError(
                f"'omit' needs NetBox 4.5.2 or newer (this is "
                f"{'.'.join(map(str, version))}); use 'fields' instead."
            )
        params["omit"] = ",".join(omit)
    if brief:
        params["brief"] = "true"
    return params


def page(
    object_type: str, data: dict[str, Any] | None, limit: int, offset: int
) -> dict:
    """Wrap a NetBox list response in the envelope every list tool returns.

    Args:
        object_type: Canonical type key of the listed objects.
        data: NetBox paginated response (``count``/``next``/``results``).
        limit: Requested page size.
        offset: Requested offset.

    Returns:
        dict: ``results`` plus ``count`` (this page), ``total`` (all matches),
            ``limit``, ``offset``, ``has_more`` and ``next_offset``.
    """
    data = data or {}
    results = data.get("results", [])
    has_more = bool(data.get("next"))
    envelope = {
        "object_type": object_type,
        "results": results,
        "count": len(results),
        "total": data.get("count"),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }
    if has_more:
        envelope["next_offset"] = offset + len(results)
    return envelope


def dry_run_result(
    action: str,
    method: str,
    endpoint: str,
    payload: Any,
    branch: str | None,
    **extra: Any,
) -> dict:
    """Describe a write that DRY_RUN_MODE stopped from being sent.

    Args:
        action: What the tool would do, e.g. ``"create"``.
        method: HTTP method that would be used.
        endpoint: API endpoint that would be called.
        payload: Request body that would be sent.
        branch: Branch the write would target.
        **extra: Tool-specific context, e.g. the current object state.

    Returns:
        dict: The simulated request, flagged ``dry_run: True``.
    """
    return {
        "dry_run": True,
        "message": "DRY_RUN_MODE is enabled - nothing was changed in NetBox. "
        "This is the request that would have been sent.",
        "action": action,
        "method": method,
        "endpoint": f"/api/{endpoint}/",
        "branch": branch,
        "payload": payload,
        **extra,
    }


def pick(obj: Any, keys: Iterable[str]) -> Any:
    """Keep only ``keys`` of a dict, in that order; anything else passes through."""
    if not isinstance(obj, dict):
        return obj
    return {key: obj[key] for key in keys if key in obj}


def project(rows: list[Any], fields: list[str] | None) -> list[Any]:
    """Trim rows to the requested fields.

    NetBox applies ``?fields=`` itself, but some plugin endpoints (e.g.
    netbox-branching) ignore it and return full objects. Trimming again here is
    a no-op when NetBox already did it, and keeps responses small when it did not.
    """
    if not fields:
        return rows
    return [pick(row, fields) for row in rows]


def summarize(obj: Any) -> Any:
    """Reduce a NetBox object to the fields needed to identify it."""
    if not isinstance(obj, dict):
        return obj
    return pick(obj, ("id", "display", "name", "url")) or obj
