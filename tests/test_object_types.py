import pytest

from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.object_types import CORE_ENDPOINTS
from netbox_mcp.object_types import all_object_types
from netbox_mcp.object_types import resolve_object_type


@pytest.mark.parametrize(
    "value",
    ["dcim.device", "DCIM.Device", "dcim/devices", "/api/dcim/devices/"],
)
def test_resolve_accepts_key_and_endpoint(value):
    key, endpoint, fallback = resolve_object_type(value)
    assert key == "dcim.device"
    assert endpoint == "dcim/devices"
    assert fallback is None


def test_resolve_matches_fallback_endpoint():
    key, endpoint, fallback = resolve_object_type("extras/object-types")
    assert key == "core.objecttype"
    assert (endpoint, fallback) == ("core/object-types", "extras/object-types")


def test_resolve_unknown_suggests_close_matches():
    with pytest.raises(ValueError, match=r"Did you mean: dcim\.device"):
        resolve_object_type("dcim.devise")


async def test_plugin_discovery_paginates_and_skips_core(config, netbox):
    config.plugin_discovery = True
    plugin = {
        "is_plugin_model": True,
        "app_label": "netbox_dns",
        "model": "zone",
        "display": "Zone",
        "rest_api_endpoint": "/api/plugins/netbox-dns/zones/",
    }
    colliding = {
        "is_plugin_model": True,
        "app_label": "dcim",
        "model": "device",
        "rest_api_endpoint": "/api/plugins/fake/devices/",
    }
    core = {"is_plugin_model": False, "app_label": "dcim", "model": "site"}
    netbox.get(
        "core/object-types",
        payload={"next": "page2", "results": [plugin, core]},
    )
    netbox.get(
        "core/object-types",
        payload={"next": None, "results": [colliding]},
    )

    async with NetBoxClient(config, transport=netbox.transport) as nb:
        assert await nb.discover_plugin_types() == 1
        key, endpoint, _ = await nb.resolve("netbox_dns.zone")

    assert key == "netbox_dns.zone"
    assert endpoint == "plugins/netbox-dns/zones"
    assert all_object_types()["dcim.device"] == CORE_ENDPOINTS["dcim.device"]


async def test_plugin_discovery_failure_keeps_core_types(config, netbox):
    netbox.get("core/object-types", status=500, payload={"detail": "boom"})
    netbox.get("core/object-types", status=500, payload={"detail": "boom"})
    async with NetBoxClient(config, transport=netbox.transport) as nb:
        assert await nb.discover_plugin_types() == 0
    assert set(all_object_types()) == set(CORE_ENDPOINTS)
