"""
Validation and encoding of NetBox list filters.

NetBox answers an unsupported filter by ignoring it, not by failing: a typo or a
Django-style lookup it does not know returns the *unfiltered* list, which a model
then reports as the answer. Rejecting the known-bad shapes up front turns that
silent over-match into an error the caller can fix.
"""

from typing import Any

# Query parameters as (name, value) pairs; the value union matches what httpx2
# accepts, so the list can be passed straight through.
QueryPairs = list[tuple[str, str | int | float | None]]

# Lookup suffixes NetBox filtersets generate. Support is still field-specific.
VALID_LOOKUP_SUFFIXES = frozenset(
    {
        "n",
        "ic",
        "nic",
        "isw",
        "nisw",
        "iew",
        "niew",
        "ie",
        "nie",
        "empty",
        "regex",
        "iregex",
        "lt",
        "lte",
        "gt",
        "gte",
    }
)

# Query parameters the tools set themselves; filters must not override them.
RESERVED_PARAMS = frozenset(
    {"limit", "offset", "fields", "omit", "brief", "ordering", "format"}
)


def validate_filters(filters: dict[str, Any]) -> None:
    """Reject filter names NetBox would silently ignore.

    Args:
        filters: Filter parameters, e.g. ``{"site_id": 1, "name__ic": "core"}``.

    Raises:
        ValueError: On a reserved parameter, the unsupported ``__in`` suffix, or
            a multi-hop lookup such as ``device__site_id``.
    """
    for name in filters:
        if name in RESERVED_PARAMS:
            raise ValueError(
                f"Invalid filter {name!r}: pass it as the tool's own "
                f"{name!r} argument, not inside filters."
            )
        if "__" not in name:
            continue

        parts = name.split("__")
        if len(parts) == 2 and parts[1] == "in":
            raise ValueError(
                f"Invalid filter {name!r}: the '__in' suffix is not supported and "
                "NetBox would ignore it. Pass a list to the field instead: "
                f"{{'{parts[0]}': [1, 2, 3]}}"
            )
        if len(parts) == 2 and parts[1] in VALID_LOOKUP_SUFFIXES:
            continue
        raise ValueError(
            f"Invalid filter {name!r}: multi-hop relationship lookups and unknown "
            "suffixes are not supported. Use a direct filter such as 'site_id', or "
            "query the related object first and filter by its ID. Valid suffixes: "
            + ", ".join(sorted(VALID_LOOKUP_SUFFIXES))
        )


def _encode(value: Any) -> str:
    """Render a filter value the way NetBox's query-string parser expects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def encode_params(params: dict[str, Any], *, drop_none: bool = True) -> QueryPairs:
    """Encode query parameters, repeating the key for list values.

    ``{"site_id": [1, 2]}`` becomes ``site_id=1&site_id=2``, which is how NetBox
    ORs multiple values of one filter.

    Args:
        params: Parameter mapping.
        drop_none: Drop ``None`` values (for optional tool arguments). When
            False, ``None`` is sent as ``null`` - NetBox's "field is empty"
            filter value, e.g. ``{"tenant_id": None}``.

    Returns:
        QueryPairs: Pairs ready to pass as httpx2 ``params``.
    """
    encoded: QueryPairs = []
    for key, value in params.items():
        if value is None and drop_none:
            continue
        if isinstance(value, list | tuple | set):
            encoded.extend((key, _encode(item)) for item in value)
        else:
            encoded.append((key, _encode(value)))
    return encoded
