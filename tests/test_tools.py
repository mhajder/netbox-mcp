import httpx2
import pytest
from fastmcp.exceptions import ToolError

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.tools.common import summarize

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
    "netbox_find_rack_space",
    "netbox_get_prefix_utilization",
}
WRITE_TOOLS = {
    "netbox_create_object",
    "netbox_update_object",
    "netbox_bulk_create_objects",
    "netbox_bulk_update_objects",
    "netbox_allocate_available",
    "netbox_delete_object",
    "netbox_bulk_delete_objects",
    "netbox_run_script",
    "netbox_connect_cable",
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


# ------------------------------------------------------------ object types


async def test_list_object_types_filters_by_app(config, client_for, netbox):
    async with client_for(config) as client:
        result = await client.call_tool("netbox_list_object_types", {"app": "ipam"})
    data = result.structured_content
    assert data["object_types"]["ipam.prefix"] == "ipam/prefixes"
    assert all(key.startswith("ipam.") for key in data["object_types"])
    assert data["count"] == len(data["object_types"])
    assert data["plugin_types"] == []
    # Without plugin discovery the registry is static.
    assert not netbox.requests


async def test_list_object_types_rediscovers_plugin_types(config, client_for, netbox):
    config.plugin_discovery = True
    zone = {
        "is_plugin_model": True,
        "app_label": "netbox_dns",
        "model": "zone",
        "rest_api_endpoint": "/api/plugins/netbox-dns/zones/",
    }
    netbox.get("core/object-types", payload={"next": None, "results": [zone]})
    async with client_for(config) as client:
        await client.call_tool("netbox_list_object_types", {})
        result = await client.call_tool(
            "netbox_list_object_types", {"app": "netbox_dns", "rediscover": True}
        )
    data = result.structured_content
    assert data["object_types"] == {"netbox_dns.zone": "plugins/netbox-dns/zones"}
    assert data["plugin_types"] == ["netbox_dns.zone"]
    # Discovery runs once, then again only because rediscover was asked for.
    assert len(netbox.sent("GET")) == 2


async def test_list_object_types_error_is_a_tool_error(client_for, netbox):
    config = NetBoxConfig(
        netbox_url="https://netbox.test", token_passthrough=True, plugin_discovery=True
    )
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"Error listing object types.*No NetBox token"
        ):
            await client.call_tool("netbox_list_object_types", {})
    assert not netbox.requests


# ----------------------------------------------------------------- reads


async def test_get_objects_joins_ordering_list(config, client_for, netbox):
    netbox.get("dcim/devices", payload={"count": 0, "results": []})
    async with client_for(config) as client:
        await client.call_tool(
            "netbox_get_objects",
            {"object_type": "dcim.device", "ordering": ["site", "-name"]},
        )
    assert ("ordering", "site,-name") in netbox.sent("GET")[0].query


@pytest.mark.parametrize(
    ("version", "status"), [("4.5.2", 200), ("4.6.0-Docker-4.0.0", 200), (None, 500)]
)
async def test_get_objects_sends_omit_when_netbox_supports_it(
    config, client_for, netbox, version, status
):
    """An unreadable version does not block omit; NetBox ignores it at worst."""
    netbox.get("status", status=status, payload={"netbox-version": version})
    netbox.get("dcim/devices", payload={"count": 0, "results": []})
    async with client_for(config) as client:
        await client.call_tool(
            "netbox_get_objects",
            {"object_type": "dcim.device", "omit": ["config_context", "comments"]},
        )
    [listing] = [r for r in netbox.sent("GET") if r.path == "/api/dcim/devices/"]
    assert ("omit", "config_context,comments") in listing.query


async def test_omit_is_refused_on_old_netbox(config, client_for, netbox):
    netbox.get("status", payload={"netbox-version": "4.4.9"})
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"'omit' needs NetBox 4\.5\.2.*this is 4\.4\.9"
        ):
            await client.call_tool(
                "netbox_get_objects", {"object_type": "dcim.device", "omit": ["x"]}
            )
    assert [r.path for r in netbox.requests] == ["/api/status/"]


async def test_fields_win_over_omit_and_brief_is_sent(config, client_for, netbox):
    netbox.get("dcim/devices/3", payload={"id": 3, "name": "sw1"})
    async with client_for(config) as client:
        await client.call_tool(
            "netbox_get_object",
            {
                "object_type": "dcim.device",
                "object_id": 3,
                "fields": ["id"],
                "omit": ["comments"],
                "brief": True,
            },
        )
    # No version lookup: omit is ignored when fields is set.
    [request] = netbox.requests
    assert dict(request.query) == {"fields": "id", "brief": "true"}


async def test_get_object_not_found(config, client_for, netbox):
    netbox.get("dcim/devices/99", status=404, payload={"detail": "No Device matches."})
    async with client_for(config) as client:
        with pytest.raises(
            ToolError,
            match=r"Error retrieving dcim\.device 99: HTTP 404.*does not exist",
        ):
            await client.call_tool(
                "netbox_get_object", {"object_type": "dcim.device", "object_id": 99}
            )


async def test_get_object_uses_legacy_endpoint_on_old_netbox(
    config, client_for, netbox
):
    netbox.get("core/object-types/4", status=404, payload={"detail": "x"})
    netbox.get("extras/object-types/4", payload={"id": 4, "model": "site"})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_get_object", {"object_type": "core.objecttype", "object_id": 4}
        )
    assert result.structured_content == {"id": 4, "model": "site"}


async def test_search_raises_transport_errors(config, client_for, netbox):
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    netbox.transport = httpx2.MockTransport(handler)
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="Error searching for 'core': ReadTimeout"):
            await client.call_tool(
                "netbox_search_objects",
                {"query": "core", "object_types": ["dcim.site"]},
            )


async def test_search_raises_on_netbox_failure(config, client_for, netbox):
    netbox.get("dcim/sites", status=500, payload={"detail": "boom"})
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="HTTP 500"):
            await client.call_tool(
                "netbox_search_objects",
                {"query": "core", "object_types": ["dcim.site"]},
            )


async def test_describe_object_type_lists_writable_fields(config, client_for, netbox):
    netbox.add(
        "OPTIONS",
        "dcim/sites",
        payload={
            "name": "Site List",
            "actions": {
                "POST": {
                    "id": {"type": "integer", "read_only": True},
                    "name": {
                        "type": "string",
                        "required": True,
                        "help_text": "Full name",
                    },
                    "status": {
                        "type": "choice",
                        "choices": [{"value": f"s{i}"} for i in range(65)],
                    },
                    "description": {"type": "string"},
                }
            },
        },
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_describe_object_type", {"object_type": "dcim/sites"}
        )
    data = result.structured_content
    assert data["object_type"] == "dcim.site"
    assert data["name"] == "Site List"
    assert data["endpoint"] == "/api/dcim/sites/"
    assert data["required"] == ["name"]
    assert set(data["fields"]) == {"name", "status", "description"}
    assert data["fields"]["name"] == {
        "type": "string",
        "required": True,
        "help": "Full name",
    }
    assert data["fields"]["status"]["choices"] == [f"s{i}" for i in range(60)]
    assert data["fields"]["status"]["choices_truncated"] == 65
    assert data["fields"]["description"] == {"type": "string"}


async def test_describe_object_type_without_add_permission(config, client_for, netbox):
    netbox.add("OPTIONS", "dcim/sites", payload={"name": "Site List", "actions": {}})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_describe_object_type", {"object_type": "dcim.site"}
        )
    data = result.structured_content
    assert data["fields"] == {}
    assert "lacks 'add' permission" in data["note"]


async def test_describe_unknown_object_type(config, client_for, netbox):
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"Error describing dcim\.devise.*dcim\.device"
        ):
            await client.call_tool(
                "netbox_describe_object_type", {"object_type": "dcim.devise"}
            )
    assert not netbox.requests


async def test_changelog_rejects_invalid_filters(config, client_for, netbox):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"Error retrieving change log.*__in"):
            await client.call_tool(
                "netbox_get_changelogs", {"filters": {"id__in": [1]}}
            )
    assert not netbox.requests


# ---------------------------------------------------------------- extras


async def test_status_reports_user_and_server_modes(config, client_for, netbox):
    config.read_only_mode = True
    netbox.get("status", payload={"netbox-version": "4.5.2"})
    netbox.get(
        "authentication-check",
        payload={"id": 7, "username": "alice", "is_active": True, "email": "a@x"},
    )
    async with client_for(config) as client:
        result = await client.call_tool("netbox_get_status", {})
    data = result.structured_content
    assert data["netbox"] == {"netbox-version": "4.5.2"}
    assert data["user"] == {"id": 7, "username": "alice", "is_active": True}
    assert data["server"]["read_only_mode"] is True
    assert data["server"]["dry_run_mode"] is False


async def test_status_without_authentication_check(config, client_for, netbox):
    """NetBox before 4.5 has no authentication-check endpoint."""
    netbox.get("status", payload={"netbox-version": "4.4.0"})
    netbox.get("authentication-check", status=404, payload={"detail": "x"})
    async with client_for(config) as client:
        result = await client.call_tool("netbox_get_status", {})
    assert result.structured_content["user"] is None


async def test_status_fails_when_token_is_rejected(config, client_for, netbox):
    netbox.get("status", payload={"netbox-version": "4.5.2"})
    netbox.get("authentication-check", status=403, payload={"detail": "no"})
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="Error retrieving NetBox status: HTTP 403"):
            await client.call_tool("netbox_get_status", {})


async def test_graphql_sends_variables(config, client_for, netbox):
    netbox.post("/graphql/", payload={"data": {"site": {"id": 1}}})
    query = "query Site($id: ID!) { site(id: $id) { id } }"
    async with client_for(config) as client:
        await client.call_tool(
            "netbox_graphql_query", {"query": query, "variables": {"id": 1}}
        )
    assert netbox.sent("POST")[0].json == {"query": query, "variables": {"id": 1}}


async def test_graphql_errors_without_data_fail(config, client_for, netbox):
    netbox.post(
        "/graphql/",
        payload={"data": None, "errors": [{"message": "Unknown field 'x'"}]},
    )
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"GraphQL errors:.*Unknown field 'x'"):
            await client.call_tool("netbox_graphql_query", {"query": "{ x }"})


@pytest.mark.parametrize(
    ("payload", "described"), [(None, "an empty body"), ([1, 2], "list")]
)
async def test_graphql_non_object_answer_is_explained(
    config, client_for, netbox, payload, described
):
    netbox.post("/graphql/", payload=payload)
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=f"Unexpected GraphQL response: .* got {described}"
        ):
            await client.call_tool("netbox_graphql_query", {"query": "{ x }"})


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            [{"vid": 10}, {"vid": 11}, {"vid": 12}],
            [{"vid": 10}, {"vid": 11}],
        ),
        ({"vid": 10}, [{"vid": 10}]),
    ],
)
async def test_get_available_lists_free_resources(
    config, client_for, netbox, payload, expected
):
    netbox.get("ipam/vlan-groups/3/available-vlans", payload=payload)
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_get_available",
            {
                "parent_type": "ipam.vlangroup",
                "parent_id": 3,
                "kind": "vlans",
                "limit": 2,
            },
        )
    assert result.structured_content == {"results": expected, "count": len(expected)}
    assert dict(netbox.sent("GET")[0].query) == {"limit": "2"}


async def test_trace_of_unconnected_port(config, client_for, netbox):
    netbox.get(
        "dcim/interfaces/7/trace",
        payload=[[[{"id": 7, "display": "eth0"}], None, []]],
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_trace_cable", {"object_type": "dcim.interface", "object_id": 7}
        )
    data = result.structured_content
    assert data["hops"] == [
        {"near_end": [{"id": 7, "display": "eth0"}], "cable": None, "far_end": []}
    ]
    assert data["complete"] is False


async def test_trace_keeps_unexpected_nodes(config, client_for, netbox):
    netbox.get("dcim/interfaces/7/trace", payload=[[["eth0"], {"id": 3}, ["eth1"]]])
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_trace_cable", {"object_type": "dcim.interface", "object_id": 7}
        )
    hop = result.structured_content["hops"][0]
    assert hop["near_end"] == ["eth0"]
    assert hop["far_end"] == ["eth1"]


async def test_trace_failure_is_a_tool_error(config, client_for, netbox):
    netbox.get("dcim/power-ports/2/trace", status=404, payload={"detail": "x"})
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"Error tracing dcim\.powerport 2: HTTP 404"
        ):
            await client.call_tool(
                "netbox_trace_cable", {"object_type": "dcim.powerport", "object_id": 2}
            )


async def test_render_config_wraps_plain_text(config, client_for, netbox):
    netbox.post(
        "virtualization/virtual-machines/5/render-config", payload="hostname vm1"
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_render_config",
            {"object_type": "virtualization.virtualmachine", "object_id": 5},
        )
    assert result.structured_content == {"configuration": "hostname vm1"}
    assert netbox.sent("POST")[0].json == {}


async def test_render_config_failure_is_a_tool_error(config, client_for, netbox):
    netbox.post(
        "dcim/devices/5/render-config",
        status=400,
        payload={"error": "No config template"},
    )
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"Error rendering config for dcim\.device 5.*No config"
        ):
            await client.call_tool(
                "netbox_render_config", {"object_type": "dcim.device", "object_id": 5}
            )


# ---------------------------------------------------------------- writes


async def test_update_object_patches_only_the_given_fields(config, client_for, netbox):
    netbox.add("PATCH", "dcim/devices/7", payload={"id": 7, "status": "offline"})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_update_object",
            {
                "object_type": "dcim.device",
                "object_id": 7,
                "data": {"status": "offline"},
            },
        )
    assert result.structured_content == {
        "success": True,
        "object_type": "dcim.device",
        "object": {"id": 7, "status": "offline"},
    }
    assert netbox.sent("PATCH")[0].json == {"status": "offline"}


async def test_update_object_failure_is_a_tool_error(config, client_for, netbox):
    netbox.add("PATCH", "dcim/devices/7", status=412, payload={"detail": "changed"})
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"Error updating dcim\.device 7: HTTP 412.*re-read"
        ):
            await client.call_tool(
                "netbox_update_object",
                {"object_type": "dcim.device", "object_id": 7, "data": {"x": 1}},
            )


async def test_bulk_create_summarizes_created_objects(config, client_for, netbox):
    netbox.post(
        "ipam/vlans",
        status=201,
        payload=[
            {"id": 1, "display": "users", "name": "users", "vid": 10, "url": "u1"},
            {"id": 2, "display": "voice", "name": "voice", "vid": 20, "url": "u2"},
        ],
    )
    items = [{"name": "users", "vid": 10}, {"name": "voice", "vid": 20}]
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_bulk_create_objects", {"object_type": "ipam.vlan", "data": items}
        )
    data = result.structured_content
    assert netbox.sent("POST")[0].json == items
    assert data["count"] == 2
    assert data["objects"] == [
        {"id": 1, "display": "users", "name": "users", "url": "u1"},
        {"id": 2, "display": "voice", "name": "voice", "url": "u2"},
    ]


async def test_bulk_create_accepts_a_single_object_answer(config, client_for, netbox):
    netbox.post("ipam/vlans", status=201, payload={"id": 1, "vid": 10})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_bulk_create_objects",
            {"object_type": "ipam.vlan", "data": [{"vid": 10}]},
        )
    data = result.structured_content
    assert data["count"] == 1
    # Nothing identifying to keep: the object is returned whole.
    assert data["objects"] == [{"id": 1}]


async def test_bulk_create_failure_is_a_tool_error(config, client_for, netbox):
    netbox.post("ipam/vlans", status=400, payload=[{}, {"vid": ["Invalid"]}])
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"Error bulk-creating ipam\.vlan.*Invalid"):
            await client.call_tool(
                "netbox_bulk_create_objects",
                {"object_type": "ipam.vlan", "data": [{"vid": 1}, {"vid": 0}]},
            )


async def test_bulk_update_patches_the_list(config, client_for, netbox):
    netbox.add(
        "PATCH",
        "dcim/sites",
        payload=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
    )
    items = [{"id": 1, "status": "active"}, {"id": 2, "status": "active"}]
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_bulk_update_objects", {"object_type": "dcim.site", "data": items}
        )
    assert netbox.sent("PATCH")[0].json == items
    assert result.structured_content["objects"] == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]


async def test_bulk_update_accepts_a_single_object_answer(config, client_for, netbox):
    netbox.add("PATCH", "dcim/sites", payload={"id": 1, "name": "a"})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_bulk_update_objects",
            {"object_type": "dcim.site", "data": [{"id": 1, "status": "active"}]},
        )
    assert result.structured_content["count"] == 1


async def test_allocate_creates_next_free_object(config, client_for, netbox):
    netbox.post(
        "ipam/prefixes/5/available-prefixes",
        status=201,
        payload={"id": 40, "prefix": "10.0.4.0/24"},
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_allocate_available",
            {
                "parent_type": "ipam.prefix",
                "parent_id": 5,
                "kind": "prefixes",
                "data": {"prefix_length": 24},
            },
        )
    assert result.structured_content == {
        "success": True,
        "object": {"id": 40, "prefix": "10.0.4.0/24"},
    }
    assert netbox.sent("POST")[0].json == {"prefix_length": 24}


async def test_allocate_without_data_sends_empty_object(config, client_for, netbox):
    netbox.post("ipam/ip-ranges/2/available-ips", status=201, payload={"id": 1})
    async with client_for(config) as client:
        await client.call_tool(
            "netbox_allocate_available",
            {"parent_type": "ipam.iprange", "parent_id": 2, "kind": "ips"},
        )
    assert netbox.sent("POST")[0].json == {}


async def test_delete_object_sends_delete(config, client_for, netbox):
    netbox.add("DELETE", "dcim/sites/4", status=204)
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_delete_object", {"object_type": "dcim.site", "object_id": 4}
        )
    assert result.structured_content == {
        "success": True,
        "object_type": "dcim.site",
        "deleted_id": 4,
    }
    assert [r.path for r in netbox.sent("DELETE")] == ["/api/dcim/sites/4/"]


async def test_bulk_delete_sends_ids(config, client_for, netbox):
    netbox.add("DELETE", "dcim/sites", status=204)
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_bulk_delete_objects",
            {"object_type": "dcim.site", "object_ids": [4, 5]},
        )
    assert result.structured_content == {
        "success": True,
        "object_type": "dcim.site",
        "count": 2,
        "deleted_ids": [4, 5],
    }
    assert netbox.sent("DELETE")[0].json == [{"id": 4}, {"id": 5}]


async def test_bulk_delete_conflict_hint(config, client_for, netbox):
    netbox.add("DELETE", "dcim/sites", status=409, payload={"detail": "dependent"})
    async with client_for(config) as client:
        with pytest.raises(
            ToolError,
            match=r"Error bulk-deleting dcim\.site.*dependent objects listed first",
        ):
            await client.call_tool(
                "netbox_bulk_delete_objects",
                {"object_type": "dcim.site", "object_ids": [4]},
            )


# ------------------------------------------------------------- branching


async def test_list_branches_sends_filters(config, client_for, netbox):
    config.branching_enabled = True
    netbox.get("plugins/branching/branches", payload={"count": 0, "results": []})
    async with client_for(config) as client:
        await client.call_tool(
            "netbox_list_branches", {"status": "ready", "name": "feat", "limit": 5}
        )
    query = dict(netbox.sent("GET")[0].query)
    assert query["status"] == "ready"
    assert query["name__ic"] == "feat"
    assert query["limit"] == "5"


async def test_list_branches_without_plugin_fails(config, client_for, netbox):
    config.branching_enabled = True
    netbox.get("plugins/branching/branches", status=404, payload={"detail": "x"})
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="Error listing branches: HTTP 404"):
            await client.call_tool("netbox_list_branches", {})


async def test_create_branch_sends_description(config, client_for, netbox):
    config.branching_enabled = True
    netbox.post("plugins/branching/branches", status=204)
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_create_branch", {"name": "feat", "description": "staging"}
        )
    assert netbox.sent("POST")[0].json == {"name": "feat", "description": "staging"}
    # An empty answer passes through instead of failing the call.
    assert result.structured_content == {"success": True, "branch": None}


async def test_create_branch_failure_is_a_tool_error(config, client_for, netbox):
    config.branching_enabled = True
    netbox.post(
        "plugins/branching/branches",
        status=400,
        payload={"name": ["branch with this name already exists."]},
    )
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"Error creating branch 'feat'.*already exists"
        ):
            await client.call_tool("netbox_create_branch", {"name": "feat"})


def test_summarize_keeps_identifying_fields_only():
    assert summarize({"id": 1, "display": "a", "vid": 10}) == {"id": 1, "display": "a"}
    # Nothing identifying, or not an object at all: returned unchanged.
    assert summarize({"vid": 10}) == {"vid": 10}
    assert summarize(None) is None
    assert summarize("created") == "created"
