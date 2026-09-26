import pytest

WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")


@pytest.fixture
def dry_config(config):
    config.dry_run_mode = True
    return config


def assert_no_writes(netbox):
    for method in WRITE_METHODS:
        assert not netbox.sent(method), f"{method} was sent in dry-run mode"


async def test_create_is_simulated(dry_config, client_for, netbox):
    async with client_for(dry_config) as client:
        result = await client.call_tool(
            "netbox_create_object",
            {"object_type": "dcim/sites", "data": {"name": "DC1", "slug": "dc1"}},
        )
    data = result.structured_content
    assert data["dry_run"] is True
    assert data["method"] == "POST"
    assert data["endpoint"] == "/api/dcim/sites/"
    assert data["object_type"] == "dcim.site"
    assert data["payload"] == {"name": "DC1", "slug": "dc1"}
    assert_no_writes(netbox)


async def test_update_shows_current_values(dry_config, client_for, netbox):
    netbox.get(
        "dcim/devices/7",
        payload={"id": 7, "display": "sw1", "status": "active", "serial": "A1"},
    )
    async with client_for(dry_config) as client:
        result = await client.call_tool(
            "netbox_update_object",
            {
                "object_type": "dcim.device",
                "object_id": 7,
                "data": {"status": "offline"},
            },
        )
    data = result.structured_content
    assert data["current_values"] == {"status": "active"}
    assert data["payload"] == {"status": "offline"}
    assert data["object"]["display"] == "sw1"
    assert_no_writes(netbox)


async def test_delete_shows_target(dry_config, client_for, netbox):
    netbox.get("ipam/prefixes/3", payload={"id": 3, "display": "10.0.0.0/24"})
    async with client_for(dry_config) as client:
        result = await client.call_tool(
            "netbox_delete_object", {"object_type": "ipam.prefix", "object_id": 3}
        )
    assert result.structured_content["object"] == {"id": 3, "display": "10.0.0.0/24"}
    assert_no_writes(netbox)


async def test_bulk_delete_reports_missing(dry_config, client_for, netbox):
    netbox.get("dcim/sites", payload={"results": [{"id": 1, "display": "a"}]})
    async with client_for(dry_config) as client:
        result = await client.call_tool(
            "netbox_bulk_delete_objects",
            {"object_type": "dcim.site", "object_ids": [1, 2]},
        )
    data = result.structured_content
    assert data["not_found"] == [2]
    assert data["payload"] == [{"id": 1}, {"id": 2}]
    assert_no_writes(netbox)


async def test_bulk_update_requires_ids(dry_config, client_for, netbox):
    from fastmcp.exceptions import ToolError

    async with client_for(dry_config) as client:
        with pytest.raises(ToolError, match="no 'id'"):
            await client.call_tool(
                "netbox_bulk_update_objects",
                {"object_type": "dcim.site", "data": [{"status": "active"}]},
            )
    assert not netbox.requests


async def test_allocate_previews_next_free(dry_config, client_for, netbox):
    netbox.get(
        "ipam/prefixes/5/available-ips",
        payload=[{"address": "10.0.0.2/24"}],
    )
    async with client_for(dry_config) as client:
        result = await client.call_tool(
            "netbox_allocate_available",
            {"parent_type": "ipam.prefix", "parent_id": 5, "kind": "ips"},
        )
    assert result.structured_content["next_available"] == {"address": "10.0.0.2/24"}
    assert_no_writes(netbox)


async def test_create_branch_is_simulated(dry_config, client_for, netbox):
    dry_config.branching_enabled = True
    async with client_for(dry_config) as client:
        result = await client.call_tool("netbox_create_branch", {"name": "feature"})
    assert result.structured_content["dry_run"] is True
    assert not netbox.requests


async def test_write_sent_when_dry_run_off(config, client_for, netbox):
    netbox.post("dcim/sites", status=201, payload={"id": 9, "name": "DC1"})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_create_object",
            {"object_type": "dcim.site", "data": {"name": "DC1", "slug": "dc1"}},
        )
    assert result.structured_content["object"]["id"] == 9
    assert len(netbox.sent("POST")) == 1
