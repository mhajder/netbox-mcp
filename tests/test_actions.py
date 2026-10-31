import pytest
from fastmcp.exceptions import ToolError

from netbox_mcp.tools import actions


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    waited: list[float] = []

    async def fake_pause(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(actions, "_pause", fake_pause)
    return waited


def job(status: str, **extra) -> dict:
    return {"id": 9, "status": {"value": status, "label": status.title()}, **extra}


async def test_run_script_posts_and_waits_for_result(
    config, client_for, netbox, pauses
):
    netbox.post("extras/scripts/cleanup.StaleIPs", payload={"result": job("pending")})
    netbox.get("core/jobs/9", payload=job("running"))
    netbox.get(
        "core/jobs/9",
        payload=job(
            "completed",
            data={"log": [{"status": "info", "message": "ok"}], "output": "done"},
        ),
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_run_script",
            {"script": "cleanup.StaleIPs", "data": {"site": 3}},
        )
    data = result.structured_content
    assert netbox.sent("POST")[0].json == {"data": {"site": 3}, "commit": False}
    assert data["committed"] is False
    assert data["job"]["status"] == "completed"
    assert data["job"]["output"] == "done"
    assert data["job"]["log"] == [{"status": "info", "message": "ok"}]
    assert "message" not in data
    assert pauses == [1, 1]


async def test_run_script_returns_job_id_on_timeout(config, client_for, netbox, pauses):
    netbox.post("extras/scripts/4", payload={"result": job("pending")})
    netbox.get("core/jobs/9", payload=job("running"))
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_run_script", {"script": 4, "commit": True, "wait_seconds": 5}
        )
    data = result.structured_content
    assert sum(pauses) == 5
    assert data["job"]["status"] == "running"
    assert "netbox_get_object('core.job', 9)" in data["message"]


async def test_run_script_without_waiting(config, client_for, netbox, pauses):
    netbox.post("extras/scripts/4", payload={"result": job("pending")})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_run_script", {"script": 4, "wait_seconds": 0}
        )
    assert result.structured_content["job"]["status"] == "pending"
    assert not pauses
    assert not netbox.sent("GET")


async def test_run_script_worker_hint(config, client_for, netbox):
    netbox.post(
        "extras/scripts/4", status=503, payload={"detail": "no workers running"}
    )
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="rqworker"):
            await client.call_tool("netbox_run_script", {"script": 4})


async def test_run_script_rejects_path_in_name(config, client_for, netbox):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"module\.ClassName"):
            await client.call_tool("netbox_run_script", {"script": "../../users"})
    assert not netbox.requests


def interface(id: int, name: str, device: str, device_id: int, cable=None) -> dict:
    return {
        "id": id,
        "display": name,
        "name": name,
        "device": {"id": device_id, "display": device},
        "cable": cable,
        "url": f"https://netbox.test/api/dcim/interfaces/{id}/",
    }


async def test_connect_cable_by_device_and_name(config, client_for, netbox):
    netbox.get(
        "dcim/interfaces",
        payload={"count": 1, "results": [interface(7, "eth1", "sw01", 1)]},
    )
    netbox.get("dcim/front-ports/42", payload=interface(42, "1", "pp01", 2))
    netbox.post("dcim/cables", status=201, payload={"id": 100, "display": "#100"})
    async with client_for(config) as client:
        result = await client.call_tool(
            "netbox_connect_cable",
            {
                "a_terminations": [
                    {"object_type": "dcim.interface", "device": "sw01", "name": "eth1"}
                ],
                "b_terminations": [{"object_type": "dcim.frontport", "object_id": 42}],
                "cable": {"type": "cat6", "label": "C-1"},
            },
        )
    lookup = netbox.sent("GET")[0]
    assert ("device", "sw01") in lookup.query
    assert ("name", "eth1") in lookup.query
    assert netbox.sent("POST")[0].json == {
        "type": "cat6",
        "label": "C-1",
        "a_terminations": [{"object_type": "dcim.interface", "object_id": 7}],
        "b_terminations": [{"object_type": "dcim.frontport", "object_id": 42}],
    }
    data = result.structured_content
    assert data["object"]["id"] == 100
    assert data["a_ends"][0]["device"] == "sw01"


async def test_connect_cable_device_id_uses_device_id_filter(
    config, client_for, netbox
):
    netbox.get(
        "dcim/interfaces",
        payload={"count": 1, "results": [interface(7, "eth1", "sw01", 5)]},
    )
    netbox.get("dcim/interfaces/8", payload=interface(8, "eth2", "sw02", 6))
    netbox.post("dcim/cables", status=201, payload={"id": 100})
    async with client_for(config) as client:
        await client.call_tool(
            "netbox_connect_cable",
            {
                "a_terminations": [
                    {"object_type": "dcim.interface", "device": 5, "name": "eth1"}
                ],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": 8}],
            },
        )
    assert ("device_id", "5") in netbox.sent("GET")[0].query


async def test_connect_cable_ambiguous_device_name(config, client_for, netbox):
    netbox.get(
        "dcim/interfaces",
        payload={
            "count": 2,
            "results": [
                interface(7, "eth1", "sw01", 1),
                interface(17, "eth1", "sw01", 11),
            ],
        },
    )
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="device's numeric ID"):
            await client.call_tool(
                "netbox_connect_cable",
                {
                    "a_terminations": [
                        {
                            "object_type": "dcim.interface",
                            "device": "sw01",
                            "name": "eth1",
                        }
                    ],
                    "b_terminations": [
                        {"object_type": "dcim.interface", "object_id": 8}
                    ],
                },
            )
    assert not netbox.sent("POST")


async def test_connect_cable_rejects_cabled_port(config, client_for, netbox):
    netbox.get(
        "dcim/interfaces/7",
        payload=interface(7, "eth1", "sw01", 1, cable={"id": 55, "display": "#55"}),
    )
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="already connected via cable #55"):
            await client.call_tool(
                "netbox_connect_cable",
                {
                    "a_terminations": [
                        {"object_type": "dcim.interface", "object_id": 7}
                    ],
                    "b_terminations": [
                        {"object_type": "dcim.interface", "object_id": 8}
                    ],
                },
            )
    assert not netbox.sent("POST")


async def test_connect_cable_port_not_found(config, client_for, netbox):
    netbox.get("dcim/interfaces", payload={"count": 0, "results": []})
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"No dcim\.interface named 'eth9'"):
            await client.call_tool(
                "netbox_connect_cable",
                {
                    "a_terminations": [
                        {
                            "object_type": "dcim.interface",
                            "device": "sw01",
                            "name": "eth9",
                        }
                    ],
                    "b_terminations": [
                        {"object_type": "dcim.interface", "object_id": 8}
                    ],
                },
            )


@pytest.mark.parametrize(
    "spec",
    [
        {"object_type": "dcim.interface"},
        {"object_type": "dcim.interface", "device": "sw01"},
        {"object_type": "dcim.powerfeed", "device": "sw01", "name": "feed"},
    ],
)
async def test_connect_cable_validates_spec(config, client_for, netbox, spec):
    async with client_for(config) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "netbox_connect_cable",
                {
                    "a_terminations": [spec],
                    "b_terminations": [
                        {"object_type": "dcim.interface", "object_id": 8}
                    ],
                },
            )
    assert not netbox.requests


async def test_connect_cable_rejects_terminations_in_attributes(
    config, client_for, netbox
):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="a_terminations"):
            await client.call_tool(
                "netbox_connect_cable",
                {
                    "a_terminations": [
                        {"object_type": "dcim.interface", "object_id": 7}
                    ],
                    "b_terminations": [
                        {"object_type": "dcim.interface", "object_id": 8}
                    ],
                    "cable": {"a_terminations": []},
                },
            )
    assert not netbox.requests


async def test_new_write_tools_hidden_in_read_only_mode(config, client_for):
    async with client_for(config, read_only=True) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "netbox_run_script" not in names
    assert "netbox_connect_cable" not in names


async def test_run_script_is_destructive(config, client_for):
    async with client_for(config) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["netbox_run_script"].annotations.destructive_hint is True
    assert tools["netbox_connect_cable"].annotations.destructive_hint is False
