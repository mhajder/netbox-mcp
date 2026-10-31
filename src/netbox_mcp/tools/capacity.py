"""
Capacity read tools: free rack space and prefix utilization.

NetBox's REST API exposes neither directly - rack elevations need combining
per face and with reservations, and prefix utilization is only computed for
the web UI - so these tools reproduce NetBox's own calculations.
"""

import asyncio
import ipaddress
from decimal import Decimal
from typing import Annotated
from typing import Any
from typing import Literal

import httpx2
from fastmcp import Context
from pydantic import Field

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import NetBoxAPIError
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.tools.common import READ_ANNOTATIONS
from netbox_mcp.tools.common import READ_ONLY_TAG
from netbox_mcp.tools.common import BranchArg
from netbox_mcp.tools.common import netbox_tool
from netbox_mcp.tools.common import pick
from netbox_mcp.tools.errors import fail

PAGE_SIZE = 1000
# Most child rows read per prefix; beyond this utilization is estimated.
MAX_CHILD_ROWS = 10_000
# Parallel NetBox requests per tool call.
CONCURRENCY = 5
MAX_FITS_LISTED = 20
HALF_UNIT = Decimal("0.5")

RACK_FIELDS = ("id", "name", "display", "status", "site", "location", "u_height")
PREFIX_FIELDS = "id,prefix,vrf,status,is_pool,mark_utilized"


async def fetch_all(
    nb: NetBoxClient, endpoint: str, params: dict[str, Any], cap: int
) -> tuple[list[Any], int]:
    """Read up to ``cap`` rows of a paginated list.

    Returns:
        tuple[list[Any], int]: The rows read and NetBox's total match count.
    """
    rows: list[Any] = []
    total = 0
    while len(rows) < cap:
        limit = min(PAGE_SIZE, cap - len(rows))
        data = await nb.get(
            endpoint, params={**params, "limit": limit, "offset": len(rows)}
        )
        data = data or {}
        page_rows = data.get("results", [])
        rows.extend(page_rows)
        total = data.get("count") or len(rows)
        if not data.get("next") or not page_rows:
            break
    return rows, max(total, len(rows))


def _merge(networks: list[Any]) -> list[Any]:
    """Merge overlapping and adjacent networks of one IP family."""
    return list(ipaddress.collapse_addresses(networks))


def _number(value: Decimal) -> int | float:
    """Render a rack unit position as 5 or 5.5, not Decimal('5.0')."""
    return int(value) if value % 1 == 0 else float(value)


def _status(obj: dict[str, Any]) -> Any:
    status = obj.get("status")
    return status.get("value") if isinstance(status, dict) else status


def _name(obj: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get("display") or obj.get("name")
    return obj


def rack_fits(
    free: set[Decimal], step: Decimal, u_height: Decimal
) -> tuple[list[Decimal], Decimal]:
    """Find where a device of ``u_height`` fits, as NetBox's get_available_units.

    Args:
        free: Free unit positions.
        step: Unit granularity of the elevation (0.5 or 1).
        u_height: Height of the device to place.

    Returns:
        tuple[list[Decimal], Decimal]: Bottom-up start positions with room for
            the device, and the height of the largest free contiguous block.
    """
    fits = []
    for start in sorted(free):
        needed = start
        while needed < start + u_height:
            if needed not in free:
                break
            needed += step
        else:
            fits.append(start)

    largest = run = Decimal(0)
    previous = None
    for unit in sorted(free):
        run = run + step if previous is not None and unit - previous == step else step
        largest = max(largest, run)
        previous = unit
    return fits, largest


def prefix_utilization(
    prefix: dict[str, Any],
    child_prefixes: list[str] | None = None,
    child_ips: list[str] | None = None,
    utilized_ranges: list[dict[str, Any]] | None = None,
    ip_total: int | None = None,
) -> dict[str, Any]:
    """Compute a prefix's utilization the way NetBox's Prefix.get_utilization does.

    Args:
        prefix: The prefix (id, prefix, vrf, status, is_pool, mark_utilized).
        child_prefixes: For a container, the child prefixes in its VRF.
        child_ips: Otherwise, the child IP addresses in its VRF.
        utilized_ranges: Otherwise, child IP ranges marked utilized.
        ip_total: How many child IPs NetBox has, when more than were read.

    Returns:
        dict: Utilization percent, used and total addresses and the method.
    """
    network = ipaddress.ip_network(prefix["prefix"], strict=False)
    result: dict[str, Any] = {
        "id": prefix.get("id"),
        "prefix": prefix["prefix"],
        "vrf": _name(prefix.get("vrf")),
        "status": _status(prefix),
        "approximate": False,
    }
    total = network.num_addresses
    if prefix.get("mark_utilized"):
        return {
            **result,
            "utilization_percent": 100.0,
            "used": total,
            "total": total,
            "method": "mark_utilized",
        }

    if _status(prefix) == "container":
        children = [ipaddress.ip_network(p, strict=False) for p in child_prefixes or []]
        same_family = [c for c in children if c.version == network.version]
        used = sum(n.num_addresses for n in _merge(same_family))
        method = "child_prefixes"
    else:
        blocks: list[Any] = []
        for row in utilized_ranges or []:
            start: Any = ipaddress.ip_interface(row["start_address"]).ip
            end: Any = ipaddress.ip_interface(row["end_address"]).ip
            blocks.extend(ipaddress.summarize_address_range(start, end))
        ranges = _merge(blocks)
        used = sum(n.num_addresses for n in ranges)
        ips = {ipaddress.ip_interface(a).ip for a in child_ips or []}
        used += sum(1 for ip in ips if not any(ip in r for r in ranges))
        if ip_total is not None and ip_total > len(child_ips or []):
            # Not every IP was read: count the rest as distinct and outside ranges.
            used += ip_total - len(child_ips or [])
            result["approximate"] = True
        if (
            network.version == 4
            and network.prefixlen < 31
            and not prefix.get("is_pool")
        ):
            total -= 2  # network and broadcast addresses
        method = "child_ips"

    percent = min(used / total * 100, 100.0) if total > 0 else 0.0
    return {
        **result,
        "utilization_percent": round(percent, 1),
        "used": used,
        "total": total,
        "method": method,
    }


def register_capacity_tools(
    mcp, config: NetBoxConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register rack-space and prefix-utilization tools with the MCP server"""

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "dcim", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_find_rack_space(
        ctx: Context,
        u_height: Annotated[
            float,
            Field(
                ge=0.5,
                le=100,
                multiple_of=0.5,
                description="Height of the device to place, in rack units.",
            ),
        ],
        site_id: Annotated[
            int | None, Field(default=None, ge=1, description="Search this site.")
        ] = None,
        location_id: Annotated[
            int | None,
            Field(default=None, ge=1, description="Search this location."),
        ] = None,
        rack_ids: Annotated[
            list[int] | None,
            Field(default=None, max_length=100, description="Search these racks."),
        ] = None,
        face: Annotated[
            Literal["front", "rear"],
            Field(default="front", description="Rack face to mount on."),
        ] = "front",
        full_depth: Annotated[
            bool,
            Field(
                default=True,
                description="The device is full depth, so the other face must be "
                "free too.",
            ),
        ] = True,
        max_racks: Annotated[
            int, Field(default=25, ge=1, le=100, description="Racks to check.")
        ] = 25,
        branch: BranchArg = None,
    ) -> dict:
        """Find racks with room for a device of a given height.

        Counts devices on the chosen face (and full-depth devices on the other)
        and rack reservations as taken. Lists the bottom-up start positions
        where the device fits. Give at least one of site_id, location_id or
        rack_ids.
        """
        try:
            if not (site_id or location_id or rack_ids):
                raise ValueError("Give site_id, location_id or rack_ids.")
            height = Decimal(str(u_height))
            faces = [face, "rear" if face == "front" else "front"]
            faces = faces if full_depth else faces[:1]
            async with NetBoxClient(config, branch, transport=transport) as nb:
                racks_page = await nb.get(
                    "dcim/racks",
                    params={
                        "site_id": site_id,
                        "location_id": location_id,
                        "id": rack_ids,
                        "fields": ",".join(RACK_FIELDS),
                        "limit": max_racks,
                    },
                )
                racks_page = racks_page or {}
                racks = racks_page.get("results", [])
                total_racks = racks_page.get("count") or len(racks)
                rack_ids_found = [rack["id"] for rack in racks]
                reservations, _ = (
                    await fetch_all(
                        nb,
                        "dcim/rack-reservations",
                        {"rack_id": rack_ids_found, "fields": "id,rack,units"},
                        MAX_CHILD_ROWS,
                    )
                    if rack_ids_found
                    else ([], 0)
                )

                limiter = asyncio.Semaphore(CONCURRENCY)

                async def elevation(rack_id: int, rack_face: str) -> list[Any]:
                    async with limiter:
                        rows, _ = await fetch_all(
                            nb,
                            f"dcim/racks/{rack_id}/elevation",
                            {
                                "face": rack_face,
                                "expand_devices": True,
                                "include_images": False,
                            },
                            PAGE_SIZE,
                        )
                    return rows

                elevations = await asyncio.gather(
                    *(elevation(rid, f) for rid in rack_ids_found for f in faces)
                )

            reserved: dict[int, set[Decimal]] = {}
            for reservation in reservations:
                rack_ref = reservation.get("rack")
                rack_id = rack_ref.get("id") if isinstance(rack_ref, dict) else rack_ref
                for unit in reservation.get("units") or []:
                    taken = reserved.setdefault(rack_id, set())
                    taken.update({Decimal(str(unit)), Decimal(str(unit)) + HALF_UNIT})

            results: list[dict[str, Any]] = []
            for index, rack in enumerate(racks):
                rack_faces = elevations[index * len(faces) : (index + 1) * len(faces)]
                units: set[Decimal] = set()
                occupied: set[Decimal] = set()
                for rows in rack_faces:
                    for row in rows:
                        unit = Decimal(str(row["id"]))
                        units.add(unit)
                        if row.get("occupied", row.get("device") is not None):
                            occupied.add(unit)
                free = units - occupied - reserved.get(rack["id"], set())
                step = HALF_UNIT if any(u % 1 for u in units) else Decimal(1)
                fits, largest = rack_fits(free, step, height)
                results.append(
                    {
                        "rack": {
                            **pick(rack, ("id", "name", "u_height")),
                            "status": _status(rack),
                            "site": _name(rack.get("site")),
                            "location": _name(rack.get("location")),
                        },
                        "free_units": _number(len(free) * step),
                        "largest_free_block": _number(largest),
                        "fit_count": len(fits),
                        "fits": [_number(u) for u in fits[:MAX_FITS_LISTED]],
                    }
                )
            results.sort(key=lambda r: (r["fit_count"] == 0, -r["largest_free_block"]))
            return {
                "u_height": _number(height),
                "face": face,
                "full_depth": full_depth,
                "racks": results,
                "racks_with_space": sum(1 for r in results if r["fit_count"]),
                "racks_checked": len(results),
                "has_more": total_racks > len(results),
            }
        except Exception as e:
            await fail(ctx, "Error finding rack space", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "ipam", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def netbox_get_prefix_utilization(
        ctx: Context,
        prefix_ids: Annotated[
            list[int],
            Field(
                min_length=1,
                max_length=25,
                description="Prefix IDs. Find them with netbox_get_objects on "
                "'ipam.prefix' (e.g. filters={'site_id': 1}).",
            ),
        ],
        branch: BranchArg = None,
    ) -> dict:
        """Get the utilization of prefixes, as shown in the NetBox UI.

        A container counts the address space of its child prefixes; any other
        prefix counts its IP addresses plus IP ranges marked utilized. NetBox's
        REST API does not return this, so it is computed from the children.
        """
        try:
            async with NetBoxClient(config, branch, transport=transport) as nb:
                limiter = asyncio.Semaphore(CONCURRENCY)

                async def one(prefix_id: int) -> dict[str, Any]:
                    async with limiter:
                        prefix = await nb.get(
                            f"ipam/prefixes/{prefix_id}",
                            params={"fields": PREFIX_FIELDS},
                        )
                        if prefix.get("mark_utilized"):
                            return prefix_utilization(prefix)
                        vrf = prefix.get("vrf")
                        scope = {
                            "vrf_id": vrf["id"] if isinstance(vrf, dict) else "null"
                        }
                        if _status(prefix) == "container":
                            rows, child_total = await fetch_all(
                                nb,
                                "ipam/prefixes",
                                {
                                    **scope,
                                    "within": prefix["prefix"],
                                    "fields": "prefix",
                                },
                                MAX_CHILD_ROWS,
                            )
                            result = prefix_utilization(
                                prefix, child_prefixes=[r["prefix"] for r in rows]
                            )
                            # Unread children can only add space: a lower bound.
                            result["approximate"] = child_total > len(rows)
                            return result
                        ranges, _ = await fetch_all(
                            nb,
                            "ipam/ip-ranges",
                            {
                                **scope,
                                "parent": prefix["prefix"],
                                "mark_utilized": True,
                                "fields": "start_address,end_address",
                            },
                            MAX_CHILD_ROWS,
                        )
                        ips, ip_total = await fetch_all(
                            nb,
                            "ipam/ip-addresses",
                            {**scope, "parent": prefix["prefix"], "fields": "address"},
                            MAX_CHILD_ROWS,
                        )
                        return prefix_utilization(
                            prefix,
                            child_ips=[r["address"] for r in ips],
                            utilized_ranges=ranges,
                            ip_total=ip_total,
                        )

                outcomes = await asyncio.gather(
                    *(one(pid) for pid in prefix_ids), return_exceptions=True
                )

            results = []
            errors: dict[int, str] = {}
            for prefix_id, outcome in zip(prefix_ids, outcomes, strict=True):
                if isinstance(outcome, NetBoxAPIError) and outcome.status == 404:
                    errors[prefix_id] = "Prefix not found."
                elif isinstance(outcome, BaseException):
                    raise outcome
                else:
                    results.append(outcome)
            response: dict[str, Any] = {"results": results, "count": len(results)}
            if errors:
                response["errors"] = errors
            return response
        except Exception as e:
            await fail(ctx, "Error computing prefix utilization", e)
