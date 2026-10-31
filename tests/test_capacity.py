from decimal import Decimal

import pytest
from fastmcp.exceptions import ToolError

from netbox_mcp.tools.capacity import prefix_utilization
from netbox_mcp.tools.capacity import rack_fits

D = Decimal


def test_rack_fits_whole_units():
    fits, largest = rack_fits({D(1), D(2), D(3), D(5), D(6)}, D(1), D(2))
    assert fits == [D(1), D(2), D(5)]
    assert largest == D(3)


def test_rack_fits_half_units():
    free = {D(1), D("1.5"), D(2), D("2.5"), D(4)}
    fits, largest = rack_fits(free, D("0.5"), D("1.5"))
    assert fits == [D(1), D("1.5")]
    assert largest == D(2)


def test_rack_fits_no_room():
    assert rack_fits({D(1), D(3)}, D(1), D(2)) == ([], D(1))


def elevation(units: int, occupied: set[int] | None = None) -> dict:
    occupied = occupied or set()
    rows = [
        {"id": u, "name": f"U{u}", "occupied": u in occupied, "device": None}
        for u in range(units, 0, -1)
    ]
    return {"count": len(rows), "next": None, "results": rows}


def racks(*ids: int, count: int | None = None) -> dict:
    rows = [
        {"id": i, "name": f"R{i}", "u_height": 4, "status": {"value": "active"}}
        for i in ids
    ]
    return {"count": count or len(rows), "next": None, "results": rows}


async def find_space(client_for, config, **args) -> dict:
    async with client_for(config) as client:
        result = await client.call_tool("netbox_find_rack_space", args)
    return result.structured_content


async def test_full_depth_needs_both_faces(config, client_for, netbox):
    netbox.get("dcim/racks", payload=racks(1))
    netbox.get("dcim/rack-reservations", payload={"count": 0, "results": []})
    netbox.get("dcim/racks/1/elevation", payload=elevation(4, {1}))  # front
    netbox.get("dcim/racks/1/elevation", payload=elevation(4, {4}))  # rear
    data = await find_space(client_for, config, u_height=1, site_id=1)
    rack = data["racks"][0]
    assert rack["fits"] == [2, 3]
    assert rack["free_units"] == 2
    faces = [dict(r.query)["face"] for r in netbox.requests if "elevation" in r.path]
    assert faces == ["front", "rear"]


async def test_half_depth_checks_one_face(config, client_for, netbox):
    netbox.get("dcim/racks", payload=racks(1))
    netbox.get("dcim/rack-reservations", payload={"count": 0, "results": []})
    netbox.get("dcim/racks/1/elevation", payload=elevation(4, {1}))
    data = await find_space(client_for, config, u_height=2, site_id=1, full_depth=False)
    assert data["racks"][0]["fits"] == [2, 3]
    assert data["racks"][0]["largest_free_block"] == 3
    assert sum("elevation" in r.path for r in netbox.requests) == 1


async def test_reservations_block_units(config, client_for, netbox):
    netbox.get("dcim/racks", payload=racks(1))
    netbox.get(
        "dcim/rack-reservations",
        payload={"count": 1, "results": [{"id": 1, "rack": {"id": 1}, "units": [3]}]},
    )
    netbox.get("dcim/racks/1/elevation", payload=elevation(4))
    data = await find_space(
        client_for, config, u_height=2, rack_ids=[1], full_depth=False
    )
    assert data["racks"][0]["fits"] == [1]


async def test_racks_with_space_first_and_truncation(config, client_for, netbox):
    netbox.get("dcim/racks", payload=racks(1, 2, count=5))
    netbox.get("dcim/rack-reservations", payload={"count": 0, "results": []})
    netbox.get("dcim/racks/1/elevation", payload=elevation(4, {1, 2, 3, 4}))
    netbox.get("dcim/racks/2/elevation", payload=elevation(4))
    data = await find_space(
        client_for, config, u_height=1, site_id=1, full_depth=False, max_racks=2
    )
    assert [r["rack"]["id"] for r in data["racks"]] == [2, 1]
    assert data["racks_with_space"] == 1
    assert data["has_more"] is True


async def test_rack_space_needs_scope(config, client_for, netbox):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="site_id"):
            await client.call_tool("netbox_find_rack_space", {"u_height": 1})
    assert not netbox.requests


def prefix(value: str, status: str = "active", **extra) -> dict:
    return {"id": 1, "prefix": value, "vrf": None, "status": {"value": status}, **extra}


def test_container_merges_overlapping_children():
    result = prefix_utilization(
        prefix("10.0.0.0/16", "container"),
        child_prefixes=["10.0.0.0/24", "10.0.0.0/25", "10.0.1.0/24"],
    )
    assert result["used"] == 512
    assert result["total"] == 65536
    assert result["utilization_percent"] == 0.8
    assert result["method"] == "child_prefixes"


@pytest.mark.parametrize(
    ("is_pool", "total", "percent"), [(False, 254, 50.0), (True, 256, 49.6)]
)
def test_ipv4_denominator(is_pool, total, percent):
    ips = [f"10.0.0.{i}/24" for i in range(1, 128)]
    result = prefix_utilization(
        prefix("10.0.0.0/24", is_pool=is_pool), child_ips=ips, utilized_ranges=[]
    )
    assert result["total"] == total
    assert result["utilization_percent"] == percent


def test_ips_inside_utilized_range_count_once():
    result = prefix_utilization(
        prefix("10.0.0.0/24"),
        child_ips=["10.0.0.15/24", "10.0.0.1/24", "10.0.0.1/24"],
        utilized_ranges=[
            {"start_address": "10.0.0.10/24", "end_address": "10.0.0.19/24"}
        ],
    )
    assert result["used"] == 11


def test_mark_utilized_is_full():
    result = prefix_utilization(prefix("10.0.0.0/24", mark_utilized=True))
    assert result["utilization_percent"] == 100.0
    assert result["method"] == "mark_utilized"


def test_unread_ips_make_result_approximate():
    result = prefix_utilization(
        prefix("2001:db8::/64"), child_ips=["2001:db8::1/64"], ip_total=20
    )
    assert result["used"] == 20
    assert result["approximate"] is True


async def test_utilization_tool_queries_children_in_vrf(config, client_for, netbox):
    netbox.get("ipam/prefixes/1", payload=prefix("10.0.0.0/30"))
    netbox.get("ipam/prefixes/2", status=404, payload={"detail": "Not found."})
    netbox.get("ipam/ip-ranges", payload={"count": 0, "results": []})
    netbox.get(
        "ipam/ip-addresses",
        payload={"count": 1, "next": None, "results": [{"address": "10.0.0.1/30"}]},
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_get_prefix_utilization", {"prefix_ids": [1, 2]}
        )
    data = result.structured_content
    assert data["results"][0]["utilization_percent"] == 50.0
    assert data["errors"] == {"2": "Prefix not found."}
    ip_query = dict(next(r for r in netbox.requests if "ip-addresses" in r.path).query)
    assert ip_query["vrf_id"] == "null"
    assert ip_query["parent"] == "10.0.0.0/30"
    range_query = dict(next(r for r in netbox.requests if "ip-ranges" in r.path).query)
    assert range_query["mark_utilized"] == "true"
