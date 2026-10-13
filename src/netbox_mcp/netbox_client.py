import asyncio
import hashlib
import logging
import os
import re
from typing import Any
from typing import ClassVar

import httpx2
from fastmcp.server.dependencies import get_access_token

from netbox_mcp.filters import QueryPairs
from netbox_mcp.filters import encode_params
from netbox_mcp.models import NetBoxConfig
from netbox_mcp.models import TransportConfig
from netbox_mcp.object_types import plugin_object_types
from netbox_mcp.object_types import resolve_object_type
from netbox_mcp.object_types import set_plugin_object_types
from netbox_mcp.retry import build_client
from netbox_mcp.retry import parse_retry_after
from netbox_mcp.retry import shared_transport
from netbox_mcp.utils import env_bool
from netbox_mcp.utils import env_float
from netbox_mcp.utils import env_int
from netbox_mcp.utils import env_str
from netbox_mcp.utils import http_host_from_env
from netbox_mcp.utils import http_port_from_env
from netbox_mcp.utils import transport_from_env

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


# Most per-caller entries kept in each class-level cache before the oldest go.
CALLER_CACHE_LIMIT = 4096


def token_fingerprint(token: str) -> str:
    """Identify a token without keeping it: its SHA-256 hex digest."""
    return hashlib.sha256(token.encode()).hexdigest()


def bounded_put(
    cache: dict[Any, Any], key: Any, value: Any, limit: int | None = None
) -> None:
    """Store ``key`` as the newest entry, dropping the oldest past ``limit``.

    ``limit`` defaults to ``CALLER_CACHE_LIMIT``.
    """
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > (CALLER_CACHE_LIMIT if limit is None else limit):
        del cache[next(iter(cache))]


def auth_header(token: str | None) -> dict[str, str]:
    """Build the Authorization header for a NetBox token.

    v2 tokens (NetBox 4.5+) look like ``nbt_<key>.<secret>`` and use the
    ``Bearer`` scheme; legacy v1 tokens use ``Token``.
    """
    if not token:
        return {}
    scheme = "Bearer" if token.startswith("nbt_") else "Token"
    return {"Authorization": f"{scheme} {token}"}


class MissingTokenError(Exception):
    """Token passthrough is on but the current request carries no NetBox token."""


class NetBoxAPIError(Exception):
    """An HTTP error response from NetBox, keeping its body.

    NetBox explains a rejected write field by field in the response body
    (``{"name": ["This field is required."]}``). A bare status code tells the
    caller nothing it can act on, so the body is carried through to the tool.
    ``retry_after`` keeps NetBox's Retry-After (seconds), e.g. on a 429.
    """

    def __init__(
        self,
        status: int,
        method: str,
        path: str,
        body: Any,
        retry_after: float | None = None,
    ):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        self.retry_after = retry_after
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
    # Keyed by caller too: with token passthrough, one user's lookup must not
    # answer for another user who may not see the same branches or types.
    # Bounded by CALLER_CACHE_LIMIT, since every new token adds entries.
    _branch_cache: ClassVar[dict[tuple[str, str, str], str]] = {}
    _plugins_discovered: ClassVar[dict[tuple[str, str], None]] = {}
    # One lock per caller: a caller's concurrent first calls share one scan,
    # while different callers' scans run side by side.
    _discovery_locks: ClassVar[dict[tuple[str, str], asyncio.Lock]] = {}
    _redirect_warned: ClassVar[set[str]] = set()

    def __init__(
        self,
        config: NetBoxConfig,
        branch: str | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        token: str | None = None,
        retry: bool = True,
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
            token: NetBox token to use instead of the one ``config`` selects.
            retry: Retry 429 and 5xx answers. Off for the token check, which
                must fail fast and pass NetBox's Retry-After on to the client.
        """
        self.config = config
        self._token = token
        self._retry = retry
        self.branch = (
            (branch or config.default_branch) if config.branching_enabled else None
        )
        self.base_url = config.netbox_url.rstrip("/")
        self._transport = transport
        self._http: httpx2.AsyncClient | None = None
        # Hash of the token in use, set on entry; keys the per-caller caches.
        self._identity = ""

    @classmethod
    def reset_cache(cls) -> None:
        """Forget cached status, branches and plugin discovery (used by tests)."""
        cls._status_cache.clear()
        cls._branch_cache.clear()
        cls._plugins_discovered.clear()
        cls._discovery_locks.clear()
        cls._redirect_warned.clear()
        set_plugin_object_types({})

    def _resolve_token(self) -> str | None:
        """Return the NetBox token for this context.

        With token passthrough the token comes from the MCP request being
        served, never from ``config.token``: a request without one must not
        borrow the server's identity.

        Raises:
            MissingTokenError: If an explicit token is empty, or passthrough is
                on and the request has no token.
        """
        if self._token is not None:
            if not self._token:
                raise MissingTokenError("An empty NetBox token was given.")
            return self._token
        if self.config.token_passthrough:
            access_token = get_access_token()
            if access_token is None or not access_token.token:
                raise MissingTokenError(
                    "No NetBox token on this request. Send your NetBox API token "
                    "as 'Authorization: Bearer <token>'."
                )
            return access_token.token
        return self.config.token

    async def __aenter__(self) -> "NetBoxClient":
        token = self._resolve_token()
        self._identity = token_fingerprint(token) if token else ""
        self._http = build_client(
            self.base_url,
            self.config.timeout,
            {"Accept": "application/json", **auth_header(token)},
            self._transport or shared_transport(self.config.verify_ssl),
            retry=self._retry,
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
                response.status_code,
                method,
                path,
                body or response.reason_phrase,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
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
        """Resolve an object type, discovering plugin types if it is unknown.

        A type already in the registry (every core type) resolves without a
        discovery scan, so a caller's first call pays for one only when it
        names a plugin type nobody has discovered yet.

        Args:
            object_type: ``app.model`` or an endpoint path.

        Returns:
            tuple[str, str, str | None]: Canonical type key, endpoint, and
                fallback endpoint.
        """
        if self.config.plugin_discovery:
            try:
                return resolve_object_type(object_type)
            except ValueError:
                await self.discover_plugin_types()
        return resolve_object_type(object_type)

    async def discover_plugin_types(self, *, force: bool = False) -> int:
        """Load plugin object types from NetBox into the type registry.

        Runs once per NetBox URL and token unless ``force`` is set. Failure is
        logged and keeps the types already known (at least the core types) -
        it never fails the tool call. With token passthrough each user sees
        only the types their token may view, so results are merged into the
        registry rather than replacing it: one user's narrower view cannot
        hide types from another.
        A user's token NetBox refuses (401/403) is then not asked again.

        A forced rediscovery replaces the registry, so uninstalled plugin types
        disappear. Other users' views are forgotten with it; ``resolve`` brings
        back a type only they may see by rediscovering when it is not found.

        Args:
            force: Rediscover even if discovery already ran.

        Returns:
            int: Number of plugin types known after discovery.
        """
        discovery_key = (self.base_url, self._identity)
        # Checked before the lock too, so callers that already ran discovery
        # never wait behind another caller's first discovery.
        if discovery_key in self._plugins_discovered and not force:
            return len(plugin_object_types())
        lock = self._discovery_locks.get(discovery_key)
        if lock is None:
            lock = asyncio.Lock()
            bounded_put(self._discovery_locks, discovery_key, lock)
        try:
            async with lock:
                if discovery_key in self._plugins_discovered and not force:
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
                    if (
                        self.config.token_passthrough
                        and isinstance(exc, NetBoxAPIError)
                        and exc.status in (401, 403)
                    ):
                        # This user's token may not read object types; asking again
                        # on every call would only repeat the failure. The server's
                        # own token keeps retrying, as its permissions may be fixed.
                        bounded_put(self._plugins_discovered, discovery_key, None)
                    return len(plugin_object_types())
                if force:
                    # Other callers rediscover lazily, through resolve or a listing.
                    for key in [
                        k for k in self._plugins_discovered if k[0] == self.base_url
                    ]:
                        del self._plugins_discovered[key]
                elif self.config.token_passthrough:
                    discovered = {**plugin_object_types(), **discovered}
                set_plugin_object_types(discovered)
                bounded_put(self._plugins_discovered, discovery_key, None)
                count = len(plugin_object_types())
                logger.info("Discovered %d plugin object types", count)
                return count
        finally:
            # Once discovered, callers take the fast path above, so the lock is
            # dropped: the dict then holds only scans still running or failed.
            if (
                discovery_key in self._plugins_discovered
                and self._discovery_locks.get(discovery_key) is lock
            ):
                del self._discovery_locks[discovery_key]

    async def resolve_branch(self, branch: str) -> str:
        """Translate a branch name into the schema ID ``X-NetBox-Branch`` needs.

        Args:
            branch: Branch name, or its 8-character schema ID.

        Returns:
            str: The branch schema ID.

        Raises:
            ValueError: If no branch matches.
        """
        cache_key = (self.base_url, self._identity, branch)
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
        bounded_put(self._branch_cache, cache_key, schema_id)
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

    # Blank means unlimited; zero or less is rejected by NetBoxConfig.
    response_max_size = (
        env_int("RESPONSE_MAX_SIZE", 0) if env_str("RESPONSE_MAX_SIZE") else None
    )

    return NetBoxConfig(
        netbox_url=env_str("NETBOX_URL") or "",
        token=env_str("NETBOX_TOKEN"),
        token_passthrough=env_bool("NETBOX_TOKEN_PASSTHROUGH", default=False),
        verify_ssl=env_bool("NETBOX_VERIFY_SSL", default=True),
        timeout=env_float("NETBOX_TIMEOUT", 30),
        branching_enabled=env_bool("NETBOX_BRANCHING_ENABLED", default=False),
        default_branch=env_str("NETBOX_BRANCH"),
        plugin_discovery=env_bool("NETBOX_PLUGIN_DISCOVERY", default=False),
        read_only_mode=env_bool("READ_ONLY_MODE", default=False),
        dry_run_mode=env_bool("DRY_RUN_MODE", default=False),
        disabled_tags=disabled_tags,
        response_max_size=response_max_size,
        rate_limit_enabled=env_bool("RATE_LIMIT_ENABLED", default=False),
        rate_limit_max_requests=env_int("RATE_LIMIT_MAX_REQUESTS", 60),
        rate_limit_window_minutes=env_int("RATE_LIMIT_WINDOW_MINUTES", 1),
        tool_search_enabled=env_bool("TOOL_SEARCH_ENABLED", default=False),
        tool_search_strategy=(
            "regex"
            if os.getenv("TOOL_SEARCH_STRATEGY", "bm25").lower() == "regex"
            else "bm25"
        ),
        tool_search_max_results=env_int("TOOL_SEARCH_MAX_RESULTS", 5),
    )


def get_transport_config_from_env() -> TransportConfig:
    """Get transport configuration from environment variables."""
    return TransportConfig(
        transport_type=transport_from_env(),
        http_host=http_host_from_env(),
        http_port=http_port_from_env(),
        http_bearer_token=env_str("MCP_HTTP_BEARER_TOKEN"),
    )
