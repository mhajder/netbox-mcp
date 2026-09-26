import pytest

from netbox_mcp.filters import encode_params
from netbox_mcp.filters import validate_filters


@pytest.mark.parametrize(
    "filters",
    [
        {"site_id": 1, "status": "active"},
        {"name__ic": "core", "vid__gte": 100},
        {"id": [1, 2, 3]},
        {"q": "router"},
        {},
    ],
)
def test_valid_filters_pass(filters):
    validate_filters(filters)


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"id__in": [1, 2]}, "__in"),
        ({"device__site_id": 1}, "multi-hop"),
        ({"interface__device__site": "a"}, "multi-hop"),
        ({"name__bogus": "x"}, "multi-hop"),
        ({"limit": 5}, "tool's own"),
        ({"fields": "id"}, "tool's own"),
    ],
)
def test_invalid_filters_rejected(filters, message):
    with pytest.raises(ValueError, match=message):
        validate_filters(filters)


def test_encode_params_expands_lists_and_booleans():
    assert encode_params({"site_id": [1, 2], "brief": True, "skip": None}) == [
        ("site_id", "1"),
        ("site_id", "2"),
        ("brief", "true"),
    ]


def test_encode_params_keeps_null_filters_when_asked():
    assert encode_params({"tenant_id": None}, drop_none=False) == [
        ("tenant_id", "null")
    ]
