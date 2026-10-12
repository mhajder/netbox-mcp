"""
Authentication for the MCP HTTP transports.
"""

import asyncio
import logging
import math
import time
from collections import OrderedDict

import httpx2
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.auth import TokenVerifier
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.authentication import AuthenticationError
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.responses import Response

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.models import TransportConfig
from netbox_mcp.netbox_client import LOOKUP_ERRORS
from netbox_mcp.netbox_client import NetBoxAPIError
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.netbox_client import bounded_put
from netbox_mcp.netbox_client import token_fingerprint
from netbox_mcp.utils import HTTP_TRANSPORTS

logger = logging.getLogger(__name__)

# Seconds a client is asked to wait when NetBox cannot verify its token and
# did not say how long to wait; NetBox's own Retry-After is capped at the max.
UNAVAILABLE_RETRY_AFTER = 5
MAX_RETRY_AFTER = 3600


class NetBoxUnavailableError(AuthenticationError):
    """NetBox could not say whether a token is valid (outage, 5xx, timeout).

    The message is for the server log only; clients get a generic answer.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def auth_error_response(conn: HTTPConnection, exc: AuthenticationError) -> Response:
    """Answer 503 when NetBox is down, so clients retry instead of re-authenticating.

    The body stays generic: an unauthenticated caller must not learn why the
    server cannot reach NetBox.
    """
    if isinstance(exc, NetBoxUnavailableError):
        retry_after = UNAVAILABLE_RETRY_AFTER
        if exc.retry_after is not None:
            retry_after = min(max(math.ceil(exc.retry_after), 1), MAX_RETRY_AFTER)
        return JSONResponse(
            {
                "error": "temporarily_unavailable",
                "error_description": "NetBox could not verify the token; retry later",
            },
            status_code=503,
            headers={"Retry-After": str(retry_after)},
        )
    return AuthenticationMiddleware.default_on_error(conn, exc)


class NetBoxTokenVerifier(TokenVerifier):
    """Accept a bearer token only if NetBox accepts it as an API token.

    The verified token is kept on the request's ``AccessToken``, where
    ``NetBoxClient`` picks it up, so every tool call runs as the caller's own
    NetBox user. Accepted tokens are cached for ``ttl`` seconds to save a NetBox
    round trip per MCP message. After that, until ``stale_ttl``, the token is
    still accepted at once while NetBox re-checks it in the background, so a
    known user never waits for, or gets a 503 from, a queued check. A revoked
    token is dropped when that re-check comes back; NetBox refuses it on every
    tool call anyway, since each call carries the token itself.

    Requests are not rate-limited before they authenticate, so the verifier
    shields NetBox itself: a rejected token is remembered for ``reject_ttl``
    seconds, and at most ``max_concurrent_checks`` checks reach NetBox at once.
    """

    def __init__(
        self,
        config: NetBoxConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        ttl: float = 60,
        stale_ttl: float = 600,
        reject_ttl: float = 30,
        max_entries: int = 1024,
        max_concurrent_checks: int = 8,
        timeout: float = 10,
    ):
        """Initialize the verifier.

        Args:
            config: NetBox connection settings.
            transport: Transport for the verification request. None uses the
                shared network transport; tests pass an httpx2.MockTransport.
            ttl: Seconds an accepted token is trusted without asking NetBox.
            stale_ttl: Seconds after its last successful check that a token is
                still accepted while being re-checked in the background, e.g.
                through a NetBox outage. Past it, requests wait for a check.
            reject_ttl: Seconds a rejected token is refused without asking
                NetBox; a token created in NetBox just after being tried
                starts working only then.
            max_entries: Accepted and rejected tokens each kept before the
                oldest is dropped.
            max_concurrent_checks: Token checks sent to NetBox at once; the
                rest queue, and those still queued at ``timeout`` get a 503.
            timeout: Most seconds one token check may take, queueing included
                (capped at ``config.timeout``). Every request with an
                uncached token waits on it, so an outage must answer 503 fast.
                The check is not retried, so NetBox's Retry-After reaches the
                client instead of being slept through.
        """
        super().__init__()
        self.config = config.model_copy(
            update={"timeout": min(config.timeout, timeout)}
        )
        self.transport = transport
        self.ttl = ttl
        self.stale_ttl = max(stale_ttl, ttl)
        self.reject_ttl = reject_ttl
        self.max_entries = max_entries
        # Fingerprint -> (token, fresh until, usable until). The cached
        # AccessToken holds the plaintext token, which tool calls need.
        self._cache: OrderedDict[str, tuple[AccessToken, float, float]] = OrderedDict()
        # Fingerprint -> monotonic time until which the token stays rejected.
        self._rejected: OrderedDict[str, float] = OrderedDict()
        # One NetBox check per token at a time: concurrent requests carrying the
        # same uncached token wait for it instead of each asking NetBox.
        self._pending: dict[str, asyncio.Task[AccessToken | None]] = {}
        self._checks = asyncio.Semaphore(max_concurrent_checks)

    def get_middleware(self) -> list:
        """FastMCP's auth middleware, with a 503 for an unreachable NetBox."""
        middleware = super().get_middleware()
        patched = False
        for entry in middleware:
            if entry.cls is AuthenticationMiddleware:
                entry.kwargs["on_error"] = auth_error_response
                patched = True
        if not patched:
            # Starlette's default handler would answer 400 with the internal
            # NetBoxUnavailableError message, so refuse to start instead.
            raise RuntimeError(
                "FastMCP's auth middleware changed: cannot install the NetBox "
                "token check's error handler"
            )
        return middleware

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the caller's access token, or None if NetBox rejects it.

        Raises:
            NetBoxUnavailableError: If NetBox could not be asked or failed to
                answer, so the token is neither accepted nor rejected.
        """
        token = token.strip()
        # An HTTP header can only carry a printable ASCII token.
        if not token or not token.isascii() or not token.isprintable():
            return None

        key = token_fingerprint(token)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[2] > now:
            access_token, fresh_until, usable_until = cached
            self._cache.move_to_end(key)
            if fresh_until <= now:
                # Stale: answer now and re-check in the background. The entry
                # counts as fresh until the check could have finished, so a
                # failed re-check is retried then, not on every request.
                self._cache[key] = (
                    access_token,
                    now + self.config.timeout,
                    usable_until,
                )
                self._check(token, key)
            return access_token
        self._cache.pop(key, None)
        if self._rejected.get(key, 0) > now:
            return None
        self._rejected.pop(key, None)

        # Shielded so one cancelled request does not fail the others waiting.
        return await asyncio.shield(self._check(token, key))

    def _check(self, token: str, key: str) -> asyncio.Task[AccessToken | None]:
        """Return the running NetBox check for ``token``, starting one if needed."""
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._ask_netbox(token, key))
            self._pending[key] = task
            task.add_done_callback(lambda done: self._finish_check(key, done))
        return task

    def _finish_check(self, key: str, task: asyncio.Task) -> None:
        """Forget a finished check, marking its error as seen.

        Every waiter may have been cancelled, and an error nobody awaited would
        otherwise be logged by asyncio as "never retrieved".
        """
        self._pending.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _ask_netbox(self, token: str, key: str) -> AccessToken | None:
        """Ask NetBox whether ``token`` is valid, caching the answer."""
        try:
            # Caps the whole check, queueing included: the fallback request
            # below would otherwise get a second full timeout of its own.
            async with (
                asyncio.timeout(self.config.timeout),
                self._checks,
                NetBoxClient(
                    self.config, token=token, transport=self.transport, retry=False
                ) as nb,
            ):
                # authentication-check was added in NetBox 4.5; users/config
                # needs a valid token too but does not name the user. Its body
                # is the user's own writable preferences, so an "id" key there
                # is attacker-chosen and must not become the identity.
                try:
                    user = await nb.get("authentication-check", use_branch=False)
                except NetBoxAPIError as exc:
                    if exc.status != 404:
                        raise
                    user = await nb.get("users/config", use_branch=False)
                    if isinstance(user, dict):
                        user = {}
        except NetBoxAPIError as exc:
            if exc.status == 404:
                # Both endpoints missing: NETBOX_URL does not point at NetBox's
                # API. That is the server's fault, not the caller's token.
                logger.error(
                    "NetBox token verification got HTTP 404 - check NETBOX_URL"
                )
                raise NetBoxUnavailableError(
                    "NetBox could not verify the token (HTTP 404); the server's "
                    "NETBOX_URL may be wrong"
                ) from exc
            if exc.status in (401, 403):
                # Debug only: every bad token would log otherwise. If all tokens
                # are refused, a proxy or redirect may be dropping the header.
                logger.debug(
                    "NetBox rejected a token: HTTP %s (if every token is rejected, "
                    "check that no proxy or redirect drops the Authorization header)",
                    exc.status,
                )
                # A background re-check may find a cached token revoked.
                self._cache.pop(key, None)
                now = time.monotonic()
                # Every entry gets the same TTL and is re-inserted at the end,
                # so the dict is ordered by expiry: drop from the front only.
                while self._rejected and next(iter(self._rejected.values())) <= now:
                    self._rejected.popitem(last=False)
                bounded_put(
                    self._rejected, key, now + self.reject_ttl, limit=self.max_entries
                )
                return None
            # Any other status (5xx, 429, or a proxy's 400/405/407) says nothing
            # about the token, so the client is told to retry, not to re-login.
            logger.warning("NetBox token verification failed: HTTP %s", exc.status)
            raise NetBoxUnavailableError(
                f"NetBox could not verify the token (HTTP {exc.status}); retry later",
                retry_after=exc.retry_after,
            ) from exc
        except LOOKUP_ERRORS as exc:
            logger.warning("NetBox token verification failed: %s", type(exc).__name__)
            raise NetBoxUnavailableError(
                f"NetBox could not verify the token ({type(exc).__name__}); retry later"
            ) from exc

        if not isinstance(user, dict):
            # Both endpoints answer a JSON object; anything else (an empty body,
            # a proxy's HTML login page) is not NetBox vouching for the token.
            logger.error(
                "NetBox token verification got a non-JSON-object answer - check "
                "NETBOX_URL"
            )
            raise NetBoxUnavailableError(
                "NetBox could not verify the token (unexpected response); the "
                "server's NETBOX_URL may be wrong"
            )
        user_id = user.get("id")
        has_id = isinstance(user_id, int) and not isinstance(user_id, bool)
        access_token = AccessToken(
            token=token,
            # The MCP SDK binds each session to its creator's client_id, so it
            # must tell users apart and stay fixed for the session's life: the
            # user ID, not the username, which an admin may rename. NetBox
            # before 4.5 names no user, so there each token is its own client.
            client_id=f"user:{user_id}" if has_id else f"token:{key}",
            scopes=[],
        )
        now = time.monotonic()
        # Drop unusable entries first: each holds a plaintext token, which should
        # not outlive ``stale_ttl`` just because the token was never sent again.
        for expired in [k for k, entry in self._cache.items() if entry[2] <= now]:
            del self._cache[expired]
        bounded_put(
            self._cache,
            key,
            (access_token, now + self.ttl, now + self.stale_ttl),
            limit=self.max_entries,
        )
        return access_token


def rate_limit_client_id(_context: object) -> str:
    """Rate-limit each authenticated NetBox user separately.

    Keyed by the verified ``client_id``: the NetBox user ID, so one user with
    several tokens still gets a single window, or the token's fingerprint on
    NetBox before 4.5. Unauthenticated transports such as stdio share one
    ``"global"`` window, as before.
    """
    access_token = get_access_token()
    if access_token is None or not access_token.token:
        return "global"
    return access_token.client_id


def build_auth_provider(
    netbox_config: NetBoxConfig, transport_config: TransportConfig
) -> AuthProvider | None:
    """Choose how MCP clients authenticate to this server.

    Args:
        netbox_config: NetBox connection settings.
        transport_config: MCP transport settings.

    Returns:
        AuthProvider | None: A NetBox token verifier with token passthrough, a
            static bearer token verifier with MCP_HTTP_BEARER_TOKEN, else None.

    Raises:
        ValueError: If token passthrough is combined with stdio or with
            MCP_HTTP_BEARER_TOKEN.
    """
    if netbox_config.token_passthrough:
        if transport_config.transport_type not in HTTP_TRANSPORTS:
            raise ValueError(
                "NETBOX_TOKEN_PASSTHROUGH requires MCP_TRANSPORT=http or sse"
            )
        if transport_config.http_bearer_token:
            raise ValueError(
                "NETBOX_TOKEN_PASSTHROUGH and MCP_HTTP_BEARER_TOKEN are mutually "
                "exclusive: both use the client's Authorization header"
            )
        return NetBoxTokenVerifier(netbox_config)

    if transport_config.http_bearer_token:
        return StaticTokenVerifier(
            tokens={
                transport_config.http_bearer_token: {
                    "client_id": "authenticated-client",
                    "scopes": ["read", "write"],
                }
            }
        )
    return None
