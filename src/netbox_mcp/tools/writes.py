"""
Generic write tools: create, update and delete any NetBox object type.

None of these carry the ``read-only`` tag, so READ_ONLY_MODE hides them all.
With DRY_RUN_MODE on, each tool resolves and validates its target, then returns
the request it would have sent instead of sending it.
"""

from typing import Annotated
from typing import Any

import httpx2
from fastmcp import Context
from pydantic import Field

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.tools.common import CREATE_ANNOTATIONS
from netbox_mcp.tools.common import DELETE_ANNOTATIONS
from netbox_mcp.tools.common import UPDATE_ANNOTATIONS
from netbox_mcp.tools.common import BranchArg
from netbox_mcp.tools.common import ObjectIdArg
from netbox_mcp.tools.common import ObjectTypeArg
from netbox_mcp.tools.common import dry_run_result
from netbox_mcp.tools.common import netbox_tool
from netbox_mcp.tools.common import summarize
from netbox_mcp.tools.errors import fail
from netbox_mcp.tools.extras import AvailableKindArg
from netbox_mcp.tools.extras import AvailableParentArg
from netbox_mcp.tools.extras import available_path

# Upper bound for one bulk request; NetBox runs a bulk write in one transaction.
MAX_BULK_ITEMS = 500

DataArg = Annotated[
    dict[str, Any],
    Field(
        description="Field values. Foreign keys take numeric IDs, e.g. {'name': "
        "'sw01', 'device_type': 4, 'role': 2, 'site': 1, 'status': 'active'}. "
        "Tags: [{'name': 'core'}] or [3]. Use netbox_describe_object_type for "
        "the fields and choices."
    ),
]
BulkDataArg = Annotated[
    list[dict[str, Any]],
    Field(min_length=1, max_length=MAX_BULK_ITEMS, description="Objects to write."),
]


def _changed_fields(current: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Return the current values of the fields an update would change."""
    return {key: current.get(key) for key in data}


def register_writes_tools(
    mcp, config: NetBoxConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register generic write tools with the MCP server"""

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def netbox_create_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        data: DataArg,
        branch: BranchArg = None,
    ) -> dict:
        """Create one NetBox object. Returns the created object.

        Look up related objects first (netbox_get_objects) and pass their IDs.
        """
        try:
            async with NetBoxClient(config, branch, transport=transport) as nb:
                key, endpoint, _ = await nb.resolve(object_type)
                if config.dry_run_mode:
                    return dry_run_result(
                        "create", "POST", endpoint, data, nb.branch, object_type=key
                    )
                await ctx.info(f"Creating {key}...")
                created = await nb.post(endpoint, data)
            return {"success": True, "object_type": key, "object": created}
        except Exception as e:
            await fail(ctx, f"Error creating {object_type}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "write"},
        annotations=UPDATE_ANNOTATIONS,
    )
    async def netbox_update_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        object_id: ObjectIdArg,
        data: Annotated[
            dict[str, Any],
            Field(
                min_length=1,
                description="Only the fields to change (PATCH), e.g. "
                "{'status': 'offline', 'description': 'RMA'}.",
            ),
        ],
        branch: BranchArg = None,
    ) -> dict:
        """Update fields of one NetBox object (partial update). Returns the
        updated object.
        """
        try:
            async with NetBoxClient(config, branch, transport=transport) as nb:
                key, endpoint, _ = await nb.resolve(object_type)
                path = f"{endpoint}/{object_id}"
                if config.dry_run_mode:
                    current = await nb.get(path)
                    return dry_run_result(
                        "update",
                        "PATCH",
                        path,
                        data,
                        nb.branch,
                        object_type=key,
                        object=summarize(current),
                        current_values=_changed_fields(current, data),
                    )
                await ctx.info(f"Updating {key} {object_id}...")
                updated = await nb.patch(path, data)
            return {"success": True, "object_type": key, "object": updated}
        except Exception as e:
            await fail(ctx, f"Error updating {object_type} {object_id}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def netbox_bulk_create_objects(
        ctx: Context,
        object_type: ObjectTypeArg,
        data: BulkDataArg,
        branch: BranchArg = None,
    ) -> dict:
        """Create many objects of one type in a single transaction (all or
        nothing). Returns the IDs of the created objects.
        """
        try:
            async with NetBoxClient(config, branch, transport=transport) as nb:
                key, endpoint, _ = await nb.resolve(object_type)
                if config.dry_run_mode:
                    return dry_run_result(
                        "bulk_create",
                        "POST",
                        endpoint,
                        data,
                        nb.branch,
                        object_type=key,
                        count=len(data),
                    )
                await ctx.info(f"Creating {len(data)} {key} objects...")
                created = await nb.post(endpoint, data)
            created = created if isinstance(created, list) else [created]
            return {
                "success": True,
                "object_type": key,
                "count": len(created),
                "objects": [summarize(obj) for obj in created],
            }
        except Exception as e:
            await fail(ctx, f"Error bulk-creating {object_type}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "write"},
        annotations=UPDATE_ANNOTATIONS,
    )
    async def netbox_bulk_update_objects(
        ctx: Context,
        object_type: ObjectTypeArg,
        data: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1,
                max_length=MAX_BULK_ITEMS,
                description="Partial updates, each with the object 'id', e.g. "
                "[{'id': 1, 'status': 'active'}, {'id': 2, 'status': 'active'}].",
            ),
        ],
        branch: BranchArg = None,
    ) -> dict:
        """Update many objects of one type in a single transaction (all or
        nothing).
        """
        try:
            missing = [i for i, item in enumerate(data) if not item.get("id")]
            if missing:
                raise ValueError(f"Items at positions {missing} have no 'id'.")
            async with NetBoxClient(config, branch, transport=transport) as nb:
                key, endpoint, _ = await nb.resolve(object_type)
                if config.dry_run_mode:
                    ids = [item["id"] for item in data]
                    current = await nb.get(
                        endpoint, params={"id": ids, "limit": len(ids)}
                    )
                    by_id = {obj["id"]: obj for obj in current.get("results", [])}
                    return dry_run_result(
                        "bulk_update",
                        "PATCH",
                        endpoint,
                        data,
                        nb.branch,
                        object_type=key,
                        count=len(data),
                        current_values={
                            item["id"]: _changed_fields(by_id[item["id"]], item)
                            for item in data
                            if item["id"] in by_id
                        },
                        not_found=[i for i in ids if i not in by_id],
                    )
                await ctx.info(f"Updating {len(data)} {key} objects...")
                updated = await nb.patch(endpoint, data)
            updated = updated if isinstance(updated, list) else [updated]
            return {
                "success": True,
                "object_type": key,
                "count": len(updated),
                "objects": [summarize(obj) for obj in updated],
            }
        except Exception as e:
            await fail(ctx, f"Error bulk-updating {object_type}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "write", "ipam"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def netbox_allocate_available(
        ctx: Context,
        parent_type: AvailableParentArg,
        parent_id: ObjectIdArg,
        kind: AvailableKindArg,
        data: Annotated[
            dict[str, Any] | list[dict[str, Any]] | None,
            Field(
                default=None,
                description="Attributes of the new object(s). Prefixes need "
                "{'prefix_length': 24}; VLANs need {'name': 'users'}. A list "
                "allocates several at once, e.g. [{'description': 'a'}, "
                "{'description': 'b'}] for two IPs.",
            ),
        ] = None,
        branch: BranchArg = None,
    ) -> dict:
        """Allocate the next free IP, prefix, VLAN or ASN from a pool and create
        it. NetBox picks the value atomically, so concurrent callers never get
        the same one.
        """
        try:
            path = available_path(parent_type, parent_id, kind)
            payload = data if data is not None else {}
            async with NetBoxClient(config, branch, transport=transport) as nb:
                if config.dry_run_mode:
                    preview = await nb.get(path, params={"limit": 1})
                    if isinstance(preview, list):
                        preview = preview[0] if preview else None
                    return dry_run_result(
                        "allocate",
                        "POST",
                        path,
                        payload,
                        nb.branch,
                        next_available=preview,
                    )
                await ctx.info(f"Allocating {kind} from {parent_type} {parent_id}...")
                created = await nb.post(path, payload)
            return {"success": True, "object": created}
        except Exception as e:
            await fail(ctx, f"Error allocating available {kind}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "delete"},
        annotations=DELETE_ANNOTATIONS,
    )
    async def netbox_delete_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        object_id: ObjectIdArg,
        branch: BranchArg = None,
    ) -> dict:
        """Permanently delete one NetBox object. Dependent objects may be deleted
        with it (e.g. a device's interfaces). Confirm with the user first.
        """
        try:
            async with NetBoxClient(config, branch, transport=transport) as nb:
                key, endpoint, _ = await nb.resolve(object_type)
                path = f"{endpoint}/{object_id}"
                if config.dry_run_mode:
                    current = await nb.get(path)
                    return dry_run_result(
                        "delete",
                        "DELETE",
                        path,
                        None,
                        nb.branch,
                        object_type=key,
                        object=summarize(current),
                    )
                await ctx.info(f"Deleting {key} {object_id}...")
                await nb.delete(path)
            return {"success": True, "object_type": key, "deleted_id": object_id}
        except Exception as e:
            await fail(ctx, f"Error deleting {object_type} {object_id}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "delete"},
        annotations=DELETE_ANNOTATIONS,
    )
    async def netbox_bulk_delete_objects(
        ctx: Context,
        object_type: ObjectTypeArg,
        object_ids: Annotated[
            list[int],
            Field(
                min_length=1,
                max_length=MAX_BULK_ITEMS,
                description="IDs of the objects to delete.",
            ),
        ],
        branch: BranchArg = None,
    ) -> dict:
        """Permanently delete many objects of one type in a single transaction.
        Confirm with the user first.
        """
        try:
            async with NetBoxClient(config, branch, transport=transport) as nb:
                key, endpoint, _ = await nb.resolve(object_type)
                if config.dry_run_mode:
                    current = await nb.get(
                        endpoint,
                        params={
                            "id": object_ids,
                            "limit": len(object_ids),
                            "brief": "true",
                        },
                    )
                    found = current.get("results", [])
                    found_ids = {obj["id"] for obj in found}
                    return dry_run_result(
                        "bulk_delete",
                        "DELETE",
                        endpoint,
                        [{"id": i} for i in object_ids],
                        nb.branch,
                        object_type=key,
                        objects=[summarize(obj) for obj in found],
                        not_found=[i for i in object_ids if i not in found_ids],
                    )
                await ctx.info(f"Deleting {len(object_ids)} {key} objects...")
                await nb.delete(endpoint, [{"id": i} for i in object_ids])
            return {
                "success": True,
                "object_type": key,
                "count": len(object_ids),
                "deleted_ids": object_ids,
            }
        except Exception as e:
            await fail(ctx, f"Error bulk-deleting {object_type}", e)
