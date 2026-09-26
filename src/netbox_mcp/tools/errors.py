"""
Surfacing upstream NetBox failures to the MCP client.

Returning ``{"error": ...}`` from a tool marks the call *successful*: the result
carries no ``isError``, so a client cannot tell a failure from an empty result.
Raising ``ToolError`` sets the error flag, and FastMCP delivers its message to
the client unmasked.
"""

import json
from typing import NoReturn

from fastmcp import Context
from fastmcp.exceptions import ToolError

from netbox_mcp.netbox_client import NetBoxAPIError

HINTS = {
    400: "Fix the fields named in the error; netbox_describe_object_type lists "
    "the writable fields and their choices.",
    401: "The NetBox token is missing, invalid or expired.",
    403: "The NetBox token lacks permission for this object type or action.",
    404: "The object or endpoint does not exist (check the ID and object type).",
    409: "The request conflicts with the current state; re-read and retry.",
    412: "The object changed since it was read; re-read and retry.",
}


def describe(exc: Exception) -> str:
    """Render an exception, keeping NetBox's error body when there is one.

    Args:
        exc: The exception raised while talking to NetBox.

    Returns:
        str: Human-readable description. For an HTTP error it includes the
            status, NetBox's JSON body and, for common statuses, a hint.
    """
    if isinstance(exc, NetBoxAPIError):
        body = exc.body
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False, default=str)
        hint = HINTS.get(exc.status)
        if exc.status == 409 and exc.method == "DELETE":
            hint = "Delete or reassign the dependent objects listed first."
        elif exc.status == 409 and "available-" in exc.path:
            hint = (
                "The pool has fewer free resources than requested; request fewer "
                "or use a larger pool (netbox_get_available lists what is free)."
            )
        text = f"HTTP {exc.status} from NetBox ({exc.method} {exc.path}): {body}"
        return f"{text}. {hint}" if hint else text
    # Some exceptions carry no message at all (asyncio.TimeoutError), so the
    # type name has to stand on its own.
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def fail(ctx: Context, action: str, exc: Exception) -> NoReturn:
    """Log a failed tool call to the session and raise it as a tool error.

    Args:
        ctx: The MCP context of the running tool.
        action: What was being attempted, e.g. ``"Error retrieving objects"``.
        exc: The exception that ended the call.

    Raises:
        ToolError: Always. This is how the failure reaches the client.
    """
    detail = f"{action}: {describe(exc)}"
    await ctx.error(detail)
    raise ToolError(detail) from exc
