import pytest
from fastmcp.exceptions import ToolError

READ_TOOLS = {
    "netbox_list_object_types",
    "netbox_get_objects",
    "netbox_get_object",
    "netbox_search_objects",
    "netbox_describe_object_type",
    "netbox_get_changelogs",
    "netbox_get_status",
    "netbox_graphql_query",
    "netbox_get_available",
    "netbox_trace_cable",
    "netbox_render_config",
}
WRITE_TOOLS = {
    "netbox_create_object",
    "netbox_update_object",
    "netbox_bulk_create_objects",
    "netbox_bulk_update_objects",
    "netbox_allocate_available",
    "netbox_delete_object",
    "netbox_bulk_delete_objects",
}
BRANCH_TOOLS = {"netbox_list_branches", "netbox_create_branch"}


async def test_all_tools_listed(config, client_for):
    async with client_for(config) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == READ_TOOLS | WRITE_TOOLS


async def test_branching_disabled_hides_branch_tools_and_argument(config, client_for):
    async with client_for(config) as client:
        tools = await client.list_tools()
    assert not BRANCH_TOOLS & {tool.name for tool in tools}
    assert all("branch" not in tool.input_schema["properties"] for tool in tools)


async def test_branching_disabled_rejects_branch_argument(config, client_for, netbox):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="branch"):
            await client.call_tool(
                "netbox_get_objects", {"object_type": "dcim.site", "branch": "x"}
            )
    assert not netbox.requests


async def test_branching_enabled_exposes_branch_tools(config, client_for):
    config.branching_enabled = True
    async with client_for(config) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS | BRANCH_TOOLS
    assert "branch" in tools["netbox_get_objects"].input_schema["properties"]


async def test_branching_enabled_read_only_keeps_list_branches(config, client_for):
    config.branching_enabled = True
    async with client_for(config, read_only=True) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == READ_TOOLS | {"netbox_list_branches"}


async def test_read_only_mode_hides_write_tools(config, client_for):
    async with client_for(config, read_only=True) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools} == READ_TOOLS
    assert all(tool.annotations.read_only_hint for tool in tools)


async def test_write_tools_are_annotated(config, client_for):
    async with client_for(config) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in WRITE_TOOLS:
        assert tools[name].annotations.read_only_hint is False
    assert tools["netbox_delete_object"].annotations.destructive_hint is True
    assert tools["netbox_bulk_delete_objects"].annotations.destructive_hint is True


async def test_get_objects_envelope_and_params(config, client_for, netbox):
    netbox.get(
        "dcim/devices",
        payload={"count": 12, "next": "x", "results": [{"id": 1}, {"id": 2}]},
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_get_objects",
            {
                "object_type": "dcim.device",
                "filters": {"site_id": [1, 2]},
                "fields": ["id", "name"],
                "limit": 2,
            },
        )
    data = result.structured_content
    assert data["total"] == 12
    assert data["count"] == 2
    assert data["has_more"] is True
    assert data["next_offset"] == 2
    params = netbox.sent("GET")[0].query
    assert ("site_id", "1") in params
    assert ("site_id", "2") in params
    assert ("fields", "id,name") in params


async def test_get_objects_rejects_in_suffix(config, client_for, netbox):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="__in"):
            await client.call_tool(
                "netbox_get_objects",
                {"object_type": "dcim.device", "filters": {"id__in": [1]}},
            )
    assert not netbox.requests


async def test_http_error_body_reaches_client(config, client_for, netbox):
    netbox.post("dcim/sites", status=400, payload={"slug": ["This field is required."]})
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"slug.*This field is required"):
            await client.call_tool(
                "netbox_create_object",
                {"object_type": "dcim.site", "data": {"name": "x"}},
            )


async def test_search_reports_errors_and_truncation(config, client_for, netbox):
    netbox.get(
        "dcim/devices",
        payload={"count": 40, "results": [{"id": 1}]},
    )
    netbox.get("dcim/sites", status=400, payload={"q": ["bad"]})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_search_objects",
            {"query": "core", "object_types": ["dcim.device", "dcim.site"], "limit": 1},
        )
    data = result.structured_content
    assert data["dcim.device"] == [{"id": 1}]
    assert data["_meta"]["truncated"] == {"dcim.device": 40}
    assert "dcim.site" in data["_meta"]["errors"]


async def test_search_raises_on_auth_failure(config, client_for, netbox):
    netbox.get("dcim/devices", status=403, payload={"detail": "denied"})
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="403"):
            await client.call_tool(
                "netbox_search_objects",
                {"query": "core", "object_types": ["dcim.device"]},
            )


@pytest.mark.parametrize(
    "query",
    [
        'mutation { create_site(name: "x") { id } }',
        "query A { site_list { id } } mutation B { x }",
        "subscription { changes }",
    ],
)
async def test_graphql_rejects_mutations(config, client_for, netbox, query):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="Only GraphQL queries"):
            await client.call_tool("netbox_graphql_query", {"query": query})
    assert not netbox.requests


async def test_graphql_allows_word_in_string(config, client_for, netbox):
    netbox.post("/graphql/", payload={"data": {"site_list": []}})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_graphql_query",
            {"query": '{ site_list(filters: {name: {exact: "mutation x"}}) { id } }'},
        )
    assert result.structured_content == {"data": {"site_list": []}}


async def test_get_available_rejects_invalid_kind(config, client_for, netbox):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="no available vlans"):
            await client.call_tool(
                "netbox_get_available",
                {"parent_type": "ipam.prefix", "parent_id": 1, "kind": "vlans"},
            )
    assert not netbox.requests


async def test_trace_uses_trace_endpoint_for_interfaces(config, client_for, netbox):
    netbox.get(
        "dcim/interfaces/7/trace",
        payload=[
            [
                [{"id": 7, "display": "eth0", "device": {"display": "sw1"}}],
                {"id": 3, "label": "C3"},
                [{"id": 9, "display": "eth1", "device": {"display": "sw2"}}],
            ]
        ],
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_trace_cable", {"object_type": "dcim.interface", "object_id": 7}
        )
    data = result.structured_content
    assert data["complete"] is True
    assert data["hops"][0]["far_end"][0]["device"] == "sw2"


@pytest.mark.parametrize(
    ("object_type", "path"),
    [
        ("dcim.frontport", "dcim/front-ports/4/paths"),
        ("dcim.rearport", "dcim/rear-ports/4/paths"),
        ("circuits.circuittermination", "circuits/circuit-terminations/4/paths"),
    ],
)
async def test_trace_uses_paths_endpoint_for_pass_through(
    config, client_for, netbox, object_type, path
):
    netbox.get(
        path,
        payload=[
            {
                "id": 1,
                "is_active": True,
                "is_complete": True,
                "is_split": False,
                "path": [[{"id": 7, "display": "eth0"}], [{"id": 3, "label": "C3"}]],
            }
        ],
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_trace_cable", {"object_type": object_type, "object_id": 4}
        )
    data = result.structured_content
    assert data["count"] == 1
    assert data["paths"][0]["nodes"][1] == [{"id": 3, "label": "C3"}]


async def test_render_config_sends_template_override(config, client_for, netbox):
    netbox.post("dcim/devices/5/render-config", payload={"content": "hostname sw1"})
    async with client_for(config) as client:
        await client.call_tool(
            "netbox_render_config",
            {
                "object_type": "dcim.device",
                "object_id": 5,
                "extra_context": {"vlan": 10},
                "config_template_id": 2,
            },
        )
    assert netbox.sent("POST")[0].json == {"vlan": 10, "config_template_id": 2}


async def test_changelog_is_compact_by_default(config, client_for, netbox):
    netbox.get("core/object-changes", payload={"count": 0, "results": []})
    async with client_for(config) as client:
        await client.call_tool("netbox_get_changelogs", {})
        await client.call_tool("netbox_get_changelogs", {"include_data": True})
    compact, full = (dict(r.query) for r in netbox.sent("GET"))
    assert "prechange_data" not in compact["fields"]
    assert "object_repr" in compact["fields"]
    assert "fields" not in full


async def test_delete_conflict_hint(config, client_for, netbox):
    netbox.add(
        "DELETE",
        "dcim/sites/1",
        status=409,
        payload={"detail": "Unable to delete object. 2 dependent objects were found"},
    )
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="dependent objects listed first"):
            await client.call_tool(
                "netbox_delete_object", {"object_type": "dcim.site", "object_id": 1}
            )


BRANCH_ROW = {
    "id": 1,
    "url": "http://x/api/plugins/branching/branches/1/",
    "display": "feat",
    "name": "feat",
    "schema_id": "abcd1234",
    "status": {"value": "ready", "label": "Ready"},
    "description": "staging",
    "owner": {"id": 1, "url": "http://x/api/users/users/1/", "display": "admin"},
    "last_sync": "2026-09-26T18:00:00Z",
    "created": "2026-09-26T17:00:00Z",
    "custom_fields": {},
    "comments": "",
    "tags": [],
}
BRANCH_EXPECTED = {
    "id": 1,
    "name": "feat",
    "schema_id": "abcd1234",
    "status": {"value": "ready", "label": "Ready"},
    "description": "staging",
    "owner": {"id": 1, "display": "admin"},
    "last_sync": "2026-09-26T18:00:00Z",
    "created": "2026-09-26T17:00:00Z",
}


async def test_list_branches_trims_fields(config, client_for, netbox):
    config.branching_enabled = True
    netbox.get(
        "plugins/branching/branches",
        payload={"count": 1, "next": None, "results": [BRANCH_ROW]},
    )
    async with client_for(config) as client:
        result = await client.call_tool("netbox_list_branches", {})
    assert result.structured_content["results"] == [BRANCH_EXPECTED]


async def test_create_branch_returns_list_shape(config, client_for, netbox):
    config.branching_enabled = True
    netbox.post("plugins/branching/branches", status=201, payload=BRANCH_ROW)
    async with client_for(config) as client:
        result = await client.call_tool("netbox_create_branch", {"name": "feat"})
    assert result.structured_content["branch"] == BRANCH_EXPECTED


async def test_list_branches_handles_empty_body(config, client_for, netbox):
    config.branching_enabled = True
    netbox.get("plugins/branching/branches", payload=None)
    async with client_for(config) as client:
        result = await client.call_tool("netbox_list_branches", {})
    assert result.structured_content["results"] == []


async def test_fields_trimmed_when_endpoint_ignores_them(config, client_for, netbox):
    config.plugin_discovery = False
    full = {"id": 1, "name": "feat", "comments": "", "custom_fields": {}}
    netbox.get("dcim/sites", payload={"count": 1, "next": None, "results": [full]})
    netbox.get("dcim/sites/1", payload=full)
    async with client_for(config) as client:
        listed = await client.call_tool(
            "netbox_get_objects", {"object_type": "dcim.site", "fields": ["id", "name"]}
        )
        single = await client.call_tool(
            "netbox_get_object",
            {"object_type": "dcim.site", "object_id": 1, "fields": ["id", "name"]},
        )
        searched = await client.call_tool(
            "netbox_search_objects",
            {"query": "feat", "object_types": ["dcim.site"], "fields": ["id"]},
        )
    assert listed.structured_content["results"] == [{"id": 1, "name": "feat"}]
    assert single.structured_content == {"id": 1, "name": "feat"}
    assert searched.structured_content["dcim.site"] == [{"id": 1}]


async def test_full_objects_kept_without_fields(config, client_for, netbox):
    full = {"id": 1, "name": "feat", "comments": ""}
    netbox.get("dcim/sites", payload={"count": 1, "next": None, "results": [full]})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_get_objects", {"object_type": "dcim.site"}
        )
    assert result.structured_content["results"] == [full]


async def test_pool_exhausted_hint(config, client_for, netbox):
    netbox.post(
        "ipam/asn-ranges/1/available-asns",
        status=409,
        payload={
            "detail": "Insufficient resources are available to satisfy the request"
        },
    )
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="request fewer or use a larger pool"):
            await client.call_tool(
                "netbox_allocate_available",
                {"parent_type": "ipam.asnrange", "parent_id": 1, "kind": "asns"},
            )
