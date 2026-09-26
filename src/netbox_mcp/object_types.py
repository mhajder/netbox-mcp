"""
Registry of NetBox object types and the REST endpoints that serve them.

Keys use NetBox's own ``app_label.model`` naming - the same strings the API returns
in ``object_type``/``changed_object_type`` fields - so a type seen in one response
can be fed straight back into another tool.

``CORE_ENDPOINTS`` covers the core models of NetBox 4.7 that have a REST
endpoint; plugin models are added at runtime by plugin discovery.
``LEGACY_ENDPOINTS`` lists where an endpoint lived in older NetBox releases; it is
tried when the current one returns 404.
"""

import difflib

# NetBox core models with a REST API endpoint: app.model -> endpoint.
CORE_ENDPOINTS: dict[str, str] = {
    # Circuits
    "circuits.circuit": "circuits/circuits",
    "circuits.circuitgroup": "circuits/circuit-groups",
    "circuits.circuitgroupassignment": "circuits/circuit-group-assignments",
    "circuits.circuittermination": "circuits/circuit-terminations",
    "circuits.circuittype": "circuits/circuit-types",
    "circuits.provider": "circuits/providers",
    "circuits.provideraccount": "circuits/provider-accounts",
    "circuits.providernetwork": "circuits/provider-networks",
    "circuits.virtualcircuit": "circuits/virtual-circuits",
    "circuits.virtualcircuittermination": "circuits/virtual-circuit-terminations",
    "circuits.virtualcircuittype": "circuits/virtual-circuit-types",
    # Core
    "core.datafile": "core/data-files",
    "core.datasource": "core/data-sources",
    "core.job": "core/jobs",
    "core.objectchange": "core/object-changes",
    "core.objecttype": "core/object-types",
    # DCIM
    "dcim.cable": "dcim/cables",
    "dcim.cablebundle": "dcim/cable-bundles",
    "dcim.cabletermination": "dcim/cable-terminations",
    "dcim.consoleport": "dcim/console-ports",
    "dcim.consoleporttemplate": "dcim/console-port-templates",
    "dcim.consoleserverport": "dcim/console-server-ports",
    "dcim.consoleserverporttemplate": "dcim/console-server-port-templates",
    "dcim.coolingfeed": "dcim/cooling-feeds",
    "dcim.coolingintake": "dcim/cooling-intakes",
    "dcim.coolingintaketemplate": "dcim/cooling-intake-templates",
    "dcim.coolingoutflow": "dcim/cooling-outflows",
    "dcim.coolingoutflowtemplate": "dcim/cooling-outflow-templates",
    "dcim.coolingsource": "dcim/cooling-sources",
    "dcim.device": "dcim/devices",
    "dcim.devicebay": "dcim/device-bays",
    "dcim.devicebaytemplate": "dcim/device-bay-templates",
    "dcim.devicerole": "dcim/device-roles",
    "dcim.devicetype": "dcim/device-types",
    "dcim.frontport": "dcim/front-ports",
    "dcim.frontporttemplate": "dcim/front-port-templates",
    "dcim.interface": "dcim/interfaces",
    "dcim.interfacetemplate": "dcim/interface-templates",
    "dcim.inventoryitem": "dcim/inventory-items",
    "dcim.inventoryitemrole": "dcim/inventory-item-roles",
    "dcim.inventoryitemtemplate": "dcim/inventory-item-templates",
    "dcim.location": "dcim/locations",
    "dcim.macaddress": "dcim/mac-addresses",
    "dcim.manufacturer": "dcim/manufacturers",
    "dcim.module": "dcim/modules",
    "dcim.modulebay": "dcim/module-bays",
    "dcim.modulebaytemplate": "dcim/module-bay-templates",
    "dcim.modulebaytype": "dcim/module-bay-types",
    "dcim.moduletype": "dcim/module-types",
    "dcim.moduletypeprofile": "dcim/module-type-profiles",
    "dcim.platform": "dcim/platforms",
    "dcim.powerfeed": "dcim/power-feeds",
    "dcim.poweroutlet": "dcim/power-outlets",
    "dcim.poweroutlettemplate": "dcim/power-outlet-templates",
    "dcim.powerpanel": "dcim/power-panels",
    "dcim.powerport": "dcim/power-ports",
    "dcim.powerporttemplate": "dcim/power-port-templates",
    "dcim.rack": "dcim/racks",
    "dcim.rackgroup": "dcim/rack-groups",
    "dcim.rackreservation": "dcim/rack-reservations",
    "dcim.rackrole": "dcim/rack-roles",
    "dcim.racktype": "dcim/rack-types",
    "dcim.rearport": "dcim/rear-ports",
    "dcim.rearporttemplate": "dcim/rear-port-templates",
    "dcim.region": "dcim/regions",
    "dcim.site": "dcim/sites",
    "dcim.sitegroup": "dcim/site-groups",
    "dcim.virtualchassis": "dcim/virtual-chassis",
    "dcim.virtualdevicecontext": "dcim/virtual-device-contexts",
    # Extras
    "extras.bookmark": "extras/bookmarks",
    "extras.configcontext": "extras/config-contexts",
    "extras.configcontextprofile": "extras/config-context-profiles",
    "extras.configtemplate": "extras/config-templates",
    "extras.customfield": "extras/custom-fields",
    "extras.customfieldchoiceset": "extras/custom-field-choice-sets",
    "extras.customlink": "extras/custom-links",
    "extras.eventrule": "extras/event-rules",
    "extras.exporttemplate": "extras/export-templates",
    "extras.imageattachment": "extras/image-attachments",
    "extras.journalentry": "extras/journal-entries",
    "extras.notification": "extras/notifications",
    "extras.notificationgroup": "extras/notification-groups",
    "extras.savedfilter": "extras/saved-filters",
    "extras.script": "extras/scripts",
    "extras.subscription": "extras/subscriptions",
    "extras.tableconfig": "extras/table-configs",
    "extras.tag": "extras/tags",
    "extras.taggeditem": "extras/tagged-objects",
    "extras.webhook": "extras/webhooks",
    # IPAM
    "ipam.aggregate": "ipam/aggregates",
    "ipam.asn": "ipam/asns",
    "ipam.asnrange": "ipam/asn-ranges",
    "ipam.fhrpgroup": "ipam/fhrp-groups",
    "ipam.fhrpgroupassignment": "ipam/fhrp-group-assignments",
    "ipam.ipaddress": "ipam/ip-addresses",
    "ipam.iprange": "ipam/ip-ranges",
    "ipam.prefix": "ipam/prefixes",
    "ipam.rir": "ipam/rirs",
    "ipam.role": "ipam/roles",
    "ipam.routetarget": "ipam/route-targets",
    "ipam.service": "ipam/services",
    "ipam.servicetemplate": "ipam/service-templates",
    "ipam.vlan": "ipam/vlans",
    "ipam.vlangroup": "ipam/vlan-groups",
    "ipam.vlantranslationpolicy": "ipam/vlan-translation-policies",
    "ipam.vlantranslationrule": "ipam/vlan-translation-rules",
    "ipam.vrf": "ipam/vrfs",
    # Tenancy
    "tenancy.contact": "tenancy/contacts",
    "tenancy.contactassignment": "tenancy/contact-assignments",
    "tenancy.contactgroup": "tenancy/contact-groups",
    "tenancy.contactrole": "tenancy/contact-roles",
    "tenancy.tenant": "tenancy/tenants",
    "tenancy.tenantgroup": "tenancy/tenant-groups",
    # Users
    "users.group": "users/groups",
    "users.objectpermission": "users/permissions",
    "users.owner": "users/owners",
    "users.ownergroup": "users/owner-groups",
    "users.token": "users/tokens",
    "users.user": "users/users",
    # Virtualization
    "virtualization.cluster": "virtualization/clusters",
    "virtualization.clustergroup": "virtualization/cluster-groups",
    "virtualization.clustertype": "virtualization/cluster-types",
    "virtualization.virtualdisk": "virtualization/virtual-disks",
    "virtualization.virtualmachine": "virtualization/virtual-machines",
    "virtualization.virtualmachinetype": "virtualization/virtual-machine-types",
    "virtualization.vminterface": "virtualization/interfaces",
    # VPN
    "vpn.ikepolicy": "vpn/ike-policies",
    "vpn.ikeproposal": "vpn/ike-proposals",
    "vpn.ipsecpolicy": "vpn/ipsec-policies",
    "vpn.ipsecprofile": "vpn/ipsec-profiles",
    "vpn.ipsecproposal": "vpn/ipsec-proposals",
    "vpn.l2vpn": "vpn/l2vpns",
    "vpn.l2vpntermination": "vpn/l2vpn-terminations",
    "vpn.tunnel": "vpn/tunnels",
    "vpn.tunnelgroup": "vpn/tunnel-groups",
    "vpn.tunneltermination": "vpn/tunnel-terminations",
    # Wireless
    "wireless.wirelesslan": "wireless/wireless-lans",
    "wireless.wirelesslangroup": "wireless/wireless-lan-groups",
    "wireless.wirelesslink": "wireless/wireless-links",
}

# Endpoints that moved between NetBox releases: app.model -> pre-move endpoint.
LEGACY_ENDPOINTS: dict[str, str] = {
    "core.objectchange": "extras/object-changes",  # moved to core in NetBox 4.1
    "core.objecttype": "extras/object-types",  # moved to core in NetBox 4.4
}

# Plugin endpoints found at runtime; kept apart from the core mapping so a
# rediscovery can replace them without touching the core types.
_plugin_endpoints: dict[str, str] = {}


def all_object_types() -> dict[str, str]:
    """Return core and discovered plugin endpoints, keyed by ``app.model``."""
    return CORE_ENDPOINTS | _plugin_endpoints


def plugin_object_types() -> dict[str, str]:
    """Return the discovered plugin endpoints, keyed by ``app.model``."""
    return dict(_plugin_endpoints)


def set_plugin_object_types(endpoints: dict[str, str]) -> None:
    """Replace the discovered plugin endpoints.

    Args:
        endpoints: ``app.model -> endpoint``. Keys that collide with a core type
            are ignored, so a plugin can never shadow a core endpoint.
    """
    _plugin_endpoints.clear()
    _plugin_endpoints.update(
        {key: ep for key, ep in endpoints.items() if key not in CORE_ENDPOINTS}
    )


def endpoint_for(object_type: str) -> str:
    """Return the endpoint of a known ``app.model`` type (KeyError if unknown)."""
    return all_object_types()[object_type]


def _normalize_endpoint(value: str) -> str:
    """Turn '/api/dcim/devices/' or 'dcim/devices' into 'dcim/devices'."""
    endpoint = value.strip().strip("/")
    return endpoint.removeprefix("api/")


def resolve_object_type(object_type: str) -> tuple[str, str, str | None]:
    """Resolve an object type given as ``app.model`` or as an API endpoint path.

    Args:
        object_type: E.g. ``"dcim.device"``, ``"dcim/devices"`` or
            ``"/api/dcim/devices/"``.

    Returns:
        tuple[str, str, str | None]: The canonical ``app.model`` key, its
            endpoint, and the pre-move endpoint for older NetBox releases.

    Raises:
        ValueError: If the type is unknown. The message suggests close matches
            so the caller can correct itself without listing every type.
    """
    types = all_object_types()
    key = object_type.strip().lower()
    if key not in types:
        endpoint = _normalize_endpoint(key)
        key = next(
            (
                type_key
                for type_key, type_endpoint in types.items()
                if endpoint in (type_endpoint, LEGACY_ENDPOINTS.get(type_key))
            ),
            key,
        )
    if key in types:
        return key, types[key], LEGACY_ENDPOINTS.get(key)

    suggestions = difflib.get_close_matches(key, types.keys(), n=5, cutoff=0.5)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ValueError(
        f"Unknown object type {object_type!r}.{hint} "
        "Use netbox_list_object_types to see every supported type."
    )
