import asyncio
import logging
import os
import re
from typing import Any
from typing import ClassVar

import httpx2

from netbox_mcp.filters import QueryPairs
from netbox_mcp.filters import encode_params
from netbox_mcp.models import NetBoxConfig
from netbox_mcp.models import TransportConfig
from netbox_mcp.object_types import plugin_object_types
from netbox_mcp.object_types import resolve_object_type
from netbox_mcp.object_types import set_plugin_object_types
from netbox_mcp.retry import build_client
from netbox_mcp.retry import shared_transport
from netbox_mcp.utils import parse_bool

logger = logging.getLogger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# netbox-branching identifies a branch in X-NetBox-Branch by its schema ID only.
SCHEMA_ID_PATTERN = re.compile(r"^[a-z0-9]{8}$")
BRANCHES_ENDPOINT = "plugins/branching/branches"
# Branch fields every tool returns; netbox-branching 1.2 ignores ?fields=, so the
# tools also trim rows to this list themselves.
BRANCH_FIELDS = (
    "id",
    "name",
    "schema_id",
    "status",
    "description",
    "owner",
    "last_sync",
    "created",
)
# Failures raised before any byte of the request reached NetBox: always safe to
# retry, even for a write.
PRE_SEND_ERRORS = (
    httpx2.ConnectError,
    httpx2.ConnectTimeout,
    httpx2.PoolTimeout,
    httpx2.UnsupportedProtocol,
)


class NetBoxAPIError(Exception):
    """An HTTP error response from NetBox, keeping its body.

    NetBox explains a rejected write field by field in the response body
    (``{"name": ["This field is required."]}``). A bare status code tells the
    caller nothing it can act on, so the body is carried through to the tool.
    """

    def __init__(self, status: int, method: str, path: str, body: Any):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"{method} {path} returned HTTP {status}: {body}")


# Failures of an optional lookup (version, plugin types) that must degrade to a
# fallback instead of failing the tool call.
LOOKUP_ERRORS = (NetBoxAPIError, httpx2.HTTPError, httpx2.InvalidURL, TimeoutError)


class AmbiguousWriteError(Exception):
    """A write whose request was sent but whose outcome is unknown.

    A timeout or dropped connection after the request left may still have been
    committed by NetBox. Retrying blindly can create a duplicate, so the caller
    is told to check first.
    """


class NetBoxClient:
    """Async client for the NetBox REST and GraphQL APIs.

    Used as ``async with NetBoxClient(config) as nb``. Each context gets a light
    HTTP client over a transport shared by the whole process, so connections and
    the TLS context are reused across tool calls. Lookups that are stable for the life of the process - the NetBox
    version, discovered plugin types and branch schema IDs - are cached on the
    class so they cost one request per server, not one per tool call.
    """

    _status_cache: ClassVar[dict[str, dict[str, Any]]] = {}
    _branch_cache: ClassVar[dict[tuple[str, str], str]] = {}
    _plugins_discovered: ClassVar[set[str]] = set()
    _discovery_lock: ClassVar[asyncio.Lock | None] = None
    _redirect_warned: ClassVar[set[str]] = set()

    def __init__(
        self,
        config: NetBoxConfig,
        branch: str | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        """Initialize the client.

        Args:
            config: NetBox connection settings.
            branch: Branch (name or schema ID) for this context. Falls back to
                ``config.default_branch``; ``None`` means the main schema. Ignored
                unless ``config.branching_enabled`` is set.
            transport: Transport to send requests through. Defaults to the
                process-wide network transport for ``config.verify_ssl``. It
                is never closed by this client.
        """
        self.config = config
        self.branch = (
            (branch or config.default_branch) if config.branching_enabled else None
        )
        self.base_url = config.netbox_url.rstrip("/")
        self._transport = transport
        self._http: httpx2.AsyncClient | None = None

    @classmethod
    def reset_cache(cls) -> None:
        """Forget cached status, branches and plugin discovery (used by tests)."""
        cls._status_cache.clear()
        cls._branch_cache.clear()
        cls._plugins_discovered.clear()
        cls._redirect_warned.clear()
        set_plugin_object_types({})

    def _auth_header(self) -> dict[str, str]:
        """Build the Authorization header for the configured token.

        v2 tokens (NetBox 4.5+) look like ``nbt_<key>.<secret>`` and use the
        ``Bearer`` scheme; legacy v1 tokens use ``Token``.
        """
        token = self.config.token
        if not token:
            return {}
        scheme = "Bearer" if token.startswith("nbt_") else "Token"
        return {"Authorization": f"{scheme} {token}"}

    async def __aenter__(self) -> "NetBoxClient":
        self._http = build_client(
            self.base_url,
            self.config.timeout,
            {"Accept": "application/json", **self._auth_header()},
            self._transport or shared_transport(self.config.verify_ssl),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        return False

    # ------------------------------------------------------------------ core

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | QueryPairs | None = None,
        json: Any = None,
        fallback_path: str | None = None,
        use_branch: bool = True,
        absolute: bool = False,
    ) -> Any:
        """Send one request and return the decoded JSON body.

        Args:
            method: HTTP method.
            path: Endpoint relative to ``/api/``, e.g. ``"dcim/devices"`` or
                ``"dcim/devices/5"``. With ``absolute=True``, relative to the
                NetBox base URL instead (for ``/graphql/``).
            params: Query parameters; a dict is encoded with list expansion.
            json: JSON request body.
            fallback_path: Tried when ``path`` returns 404, for endpoints that
                moved between NetBox releases.
            use_branch: Send the ``X-NetBox-Branch`` header for the active
                branch. Disabled for the branch lookup itself.
            absolute: Treat ``path`` as relative to the base URL, not ``/api/``.

        Returns:
            Any: Decoded JSON, or ``None`` for an empty successful response.

        Raises:
            NetBoxAPIError: On any HTTP error status.
            AmbiguousWriteError: When a write was sent but its outcome is unknown.
            TimeoutError: When a read does not finish within ``config.timeout``.
            ValueError: When a redirect turned a write into a GET.
        """
        if self._http is None:
            raise RuntimeError("NetBoxClient must be used as an async context manager")

        headers: dict[str, str] = {}
        if use_branch and self.branch:
            headers["X-NetBox-Branch"] = await self.resolve_branch(self.branch)

        if isinstance(params, dict):
            params = encode_params(params)

        prefix = "" if absolute else "/api"
        url = f"{prefix}/{path.strip('/')}/"
        timeout = self.config.timeout
        try:
            # httpx2's timeout applies to each connect/read/write step; this caps
            # the whole call, retries included, as NETBOX_TIMEOUT promises.
            async with asyncio.timeout(timeout):
                response = await self._http.request(
                    method, url, params=params or None, json=json, headers=headers
                )
        except PRE_SEND_ERRORS:
            # Nothing reached NetBox, so a retry is safe.
            raise
        except (httpx2.RequestError, TimeoutError) as exc:
            if method in WRITE_METHODS:
                raise AmbiguousWriteError(
                    f"{method} {path} was sent but no valid response arrived "
                    f"({type(exc).__name__}: {exc}). The change MAY have been "
                    "applied - check the current state in NetBox before retrying."
                ) from exc
            if isinstance(exc, TimeoutError):
                raise TimeoutError(
                    f"NetBox did not answer {method} {path} within {timeout}s"
                ) from exc
            raise

        if response.history:
            self._check_redirect(method, path, response)

        if response.status_code == 404 and fallback_path:
            logger.debug("%s returned 404, trying %s", path, fallback_path)
            return await self.request(
                method,
                fallback_path,
                params=params,
                json=json,
                use_branch=use_branch,
                absolute=absolute,
            )
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = response.text[:2000]
        else:
            body = None
        # Checked before the empty-body case: a proxy's bare 502 is still an error.
        if response.status_code >= 400:
            raise NetBoxAPIError(
                response.status_code, method, path, body or response.reason_phrase
            )
        return body

    def _check_redirect(
        self, method: str, path: str, response: httpx2.Response
    ) -> None:
        """Reject a write a redirect turned into a GET, and flag NETBOX_URL once.

        A 301/302/303 answer to a POST/PATCH/DELETE is followed as a GET, so the
        write never happened even though the final response succeeds.

        Raises:
            ValueError: If the method changed along the redirect chain.
        """
        final_url = response.request.url
        if response.request.method != method:
            raise ValueError(
                f"{method} {path} was redirected to {final_url} and turned into a "
                f"{response.request.method}, so the change was NOT applied. Set "
                "NETBOX_URL to the final address (e.g. https:// instead of http://)."
            )
        if self.base_url not in self._redirect_warned:
            self._redirect_warned.add(self.base_url)
            logger.warning(
                "NetBox redirects %s to %s - set NETBOX_URL to the final address "
                "to avoid an extra round trip on every call.",
                response.history[0].request.url,
                final_url,
            )

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, json: Any, **kwargs: Any) -> Any:
        return await self.request("POST", path, json=json, **kwargs)

    async def patch(self, path: str, json: Any, **kwargs: Any) -> Any:
        return await self.request("PATCH", path, json=json, **kwargs)

    async def delete(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, json=json, **kwargs)

    async def options(self, path: str, **kwargs: Any) -> Any:
        return await self.request("OPTIONS", path, **kwargs)

    # ------------------------------------------------------------ discovery

    async def status(self) -> dict[str, Any]:
        """Return ``/api/status/``, cached per NetBox URL."""
        cached = self._status_cache.get(self.base_url)
        if cached is None:
            cached = await self.get("status", use_branch=False)
            self._status_cache[self.base_url] = cached
        return cached

    async def version(self) -> tuple[int, ...] | None:
        """Return the NetBox version as a tuple, or None if it cannot be read.

        ``netbox-version`` may carry a suffix (``"4.3.0-Docker-3.3.0"``), so only
        the leading dotted digits are parsed.
        """
        try:
            status = await self.status()
        except LOOKUP_ERRORS as exc:
            logger.warning("Could not read NetBox version: %s", exc)
            return None
        raw = str(status.get("netbox-version", "")) if isinstance(status, dict) else ""
        match = re.match(r"(\d+(?:\.\d+)*)", raw)
        return tuple(int(part) for part in match.group(1).split(".")) if match else None

    async def resolve(self, object_type: str) -> tuple[str, str, str | None]:
        """Resolve an object type, discovering plugin types first if enabled.

        Args:
            object_type: ``app.model`` or an endpoint path.

        Returns:
            tuple[str, str, str | None]: Canonical type key, endpoint, and
                fallback endpoint.
        """
        if self.config.plugin_discovery:
            await self.discover_plugin_types()
        return resolve_object_type(object_type)

    async def discover_plugin_types(self, *, force: bool = False) -> int:
        """Load plugin object types from NetBox into the type registry.

        Runs once per NetBox URL unless ``force`` is set. Failure is logged and
        leaves only the core types available - it never fails the tool call.

        Args:
            force: Rediscover even if discovery already ran.

        Returns:
            int: Number of plugin types known after discovery.
        """
        if NetBoxClient._discovery_lock is None:
            NetBoxClient._discovery_lock = asyncio.Lock()
        async with NetBoxClient._discovery_lock:
            if self.base_url in self._plugins_discovered and not force:
                return len(plugin_object_types())
            discovered: dict[str, str] = {}
            try:
                offset = 0
                while True:
                    page = await self.get(
                        "core/object-types",
                        params={"limit": 1000, "offset": offset},
                        fallback_path="extras/object-types",
                        use_branch=False,
                    )
                    rows = page.get("results", [])
                    for row in rows:
                        entry = _plugin_type_entry(row)
                        if entry:
                            discovered[entry[0]] = entry[1]
                    # Advance by rows received: NetBox clamps the page size to
                    # MAX_PAGE_SIZE, so a fixed step would skip rows.
                    if not page.get("next") or not rows:
                        break
                    offset += len(rows)
            except LOOKUP_ERRORS as exc:
                logger.warning("Plugin discovery failed, using core types: %s", exc)
                return 0
            set_plugin_object_types(discovered)
            self._plugins_discovered.add(self.base_url)
            count = len(plugin_object_types())
            logger.info("Discovered %d plugin object types", count)
            return count

    async def resolve_branch(self, branch: str) -> str:
        """Translate a branch name into the schema ID ``X-NetBox-Branch`` needs.

        Args:
            branch: Branch name, or its 8-character schema ID.

        Returns:
            str: The branch schema ID.

        Raises:
            ValueError: If no branch matches.
        """
        cache_key = (self.base_url, branch)
        if cache_key in self._branch_cache:
            return self._branch_cache[cache_key]

        try:
            page = await self.get(
                BRANCHES_ENDPOINT,
                params={"name": branch, "fields": ",".join(BRANCH_FIELDS)},
                use_branch=False,
            )
        except NetBoxAPIError as exc:
            if exc.status == 404:
                raise ValueError(
                    "Branch support needs the netbox-branching plugin, which this "
                    "NetBox does not have."
                ) from exc
            raise
        rows = page.get("results", [])
        if rows:
            schema_id = rows[0]["schema_id"]
        elif SCHEMA_ID_PATTERN.match(branch):
            schema_id = branch
        else:
            raise ValueError(
                f"Branch {branch!r} not found. Use netbox_list_branches to see "
                "available branches."
            )
        self._branch_cache[cache_key] = schema_id
        return schema_id


def _plugin_type_entry(row: dict[str, Any]) -> tuple[str, str] | None:
    """Build a registry entry from one object-types row, if it is a plugin model."""
    if not row.get("is_plugin_model"):
        return None
    rest_url = row.get("rest_api_endpoint")
    app_label = row.get("app_label")
    model = row.get("model")
    if not rest_url or not app_label or not model:
        return None
    # "/api/plugins/netbox-dns/zones/" -> "plugins/netbox-dns/zones"
    endpoint = rest_url.strip("/").removeprefix("api/")
    if not endpoint.startswith("plugins/"):
        return None
    return f"{app_label}.{model}", endpoint


def get_netbox_config_from_env() -> NetBoxConfig:
    """Get NetBox configuration from environment variables."""
    # Parse disabled tags from comma-separated string
    disabled_tags_str = os.getenv("DISABLED_TAGS", "")
    disabled_tags = set()
    if disabled_tags_str.strip():
        disabled_tags = {
            tag.strip() for tag in disabled_tags_str.split(",") if tag.strip()
        }

    response_max_size = os.getenv("RESPONSE_MAX_SIZE", "").strip()

    return NetBoxConfig(
        netbox_url=os.getenv("NETBOX_URL", ""),
        token=(os.getenv("NETBOX_TOKEN") or "").strip() or None,
        verify_ssl=parse_bool(os.getenv("NETBOX_VERIFY_SSL"), default=True),
        timeout=float(os.getenv("NETBOX_TIMEOUT", "30")),
        branching_enabled=parse_bool(
            os.getenv("NETBOX_BRANCHING_ENABLED"), default=False
        ),
        default_branch=(os.getenv("NETBOX_BRANCH") or "").strip() or None,
        plugin_discovery=parse_bool(
            os.getenv("NETBOX_PLUGIN_DISCOVERY"), default=False
        ),
        read_only_mode=parse_bool(os.getenv("READ_ONLY_MODE"), default=False),
        dry_run_mode=parse_bool(os.getenv("DRY_RUN_MODE"), default=False),
        disabled_tags=disabled_tags,
        response_max_size=int(response_max_size) if response_max_size else None,
        rate_limit_enabled=parse_bool(os.getenv("RATE_LIMIT_ENABLED"), default=False),
        rate_limit_max_requests=int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "60")),
        rate_limit_window_minutes=int(os.getenv("RATE_LIMIT_WINDOW_MINUTES", "1")),
        tool_search_enabled=parse_bool(os.getenv("TOOL_SEARCH_ENABLED"), default=False),
        tool_search_strategy=(
            "regex"
            if os.getenv("TOOL_SEARCH_STRATEGY", "bm25").lower() == "regex"
            else "bm25"
        ),
        tool_search_max_results=int(os.getenv("TOOL_SEARCH_MAX_RESULTS", "5")),
    )


def get_transport_config_from_env() -> TransportConfig:
    """Get transport configuration from environment variables."""
    http_bearer_token = os.getenv("MCP_HTTP_BEARER_TOKEN")
    if http_bearer_token is not None:
        http_bearer_token = http_bearer_token.strip() or None

    return TransportConfig(
        transport_type=os.getenv("MCP_TRANSPORT", "stdio").lower(),
        http_host=os.getenv("MCP_HTTP_HOST", "127.0.0.1"),
        http_port=int(os.getenv("MCP_HTTP_PORT", "8000")),
        http_bearer_token=http_bearer_token,
    )
