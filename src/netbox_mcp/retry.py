"""
Retrying transport for the NetBox API.

Retries live in the HTTP layer rather than in each tool: it is the one place
every request already passes through, and it keeps ``Retry-After`` next to the
response that carries it.

Only responses that can plausibly succeed on a second attempt are retried - 429
and 5xx. A 5xx on a POST is *not* retried: NetBox may have committed the create
before the proxy in front of it failed, and replaying the request would create a
duplicate. A 429 is always safe to replay, because the request was rejected
before it reached a view.
"""

import asyncio
import logging

import httpx2

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Methods whose effect is the same when applied twice, so a 5xx may be replayed.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"})

MAX_ATTEMPTS = 3
BASE_BACKOFF = 0.5
MAX_DELAY = 10.0


def _retry_after_seconds(response: httpx2.Response) -> float | None:
    """Read a Retry-After header, in seconds, if the server sent a usable one.

    Args:
        response: The response to inspect.

    Returns:
        float | None: Delay in seconds, or None if absent, unparsable, or longer
            than a tool call should reasonably block for.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        # Only the delta-seconds form is handled; the HTTP-date form is rare
        # here and not worth the parsing risk.
        delay = float(raw.strip())
    except ValueError:
        return None
    if delay < 0 or delay > MAX_DELAY:
        return None
    return delay


class RetryTransport(httpx2.AsyncBaseTransport):
    """Transport wrapper that retries NetBox requests on transient failures."""

    def __init__(self, wrapped: httpx2.AsyncBaseTransport, *, owns_wrapped: bool):
        """Wrap a transport.

        Args:
            wrapped: The transport that actually sends requests.
            owns_wrapped: Close ``wrapped`` when this transport is closed. False
                for a transport shared between clients, which must outlive them.
        """
        self._wrapped = wrapped
        self._owns_wrapped = owns_wrapped

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Send a request, retrying it when the server reports a transient failure.

        Args:
            request: The outgoing request, replayed unchanged on each attempt.

        Returns:
            httpx2.Response: The first non-retryable response, or the last attempt.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = await self._wrapped.handle_async_request(request)
            if response.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                return response
            if response.status_code != 429 and request.method not in IDEMPOTENT_METHODS:
                return response

            delay = _retry_after_seconds(response)
            if delay is None:
                delay = min(BASE_BACKOFF * 2 ** (attempt - 1), MAX_DELAY)
            await response.aclose()

            logger.warning(
                "NetBox API returned HTTP %d (attempt %d/%d), retrying in %.1fs",
                response.status_code,
                attempt,
                MAX_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

        raise RuntimeError("unreachable: retry loop always returns")

    async def aclose(self) -> None:
        if self._owns_wrapped:
            await self._wrapped.aclose()


# One network transport per TLS setting for the whole process. Building one
# creates an SSL context (loading the trust store) and a connection pool, so
# doing it per tool call would add a fresh TLS handshake to every call.
_shared_transports: dict[bool, httpx2.AsyncHTTPTransport] = {}


def shared_transport(verify_ssl: bool) -> httpx2.AsyncHTTPTransport:
    """Return the process-wide network transport for a TLS setting.

    With ``verify_ssl`` on, certificates are checked against the operating
    system trust store (via truststore), so a NetBox behind an internal CA works
    once that CA is trusted by the OS. ``SSL_CERT_FILE``/``SSL_CERT_DIR`` still
    take precedence when set.

    The pooled connections belong to the event loop that opened them, which is
    fine for the server's single loop.

    Args:
        verify_ssl: Whether to validate the server's TLS certificate.

    Returns:
        httpx2.AsyncHTTPTransport: A transport shared by every NetBox client.
    """
    transport = _shared_transports.get(verify_ssl)
    if transport is None:
        transport = httpx2.AsyncHTTPTransport(verify=verify_ssl)
        _shared_transports[verify_ssl] = transport
    return transport


def build_client(
    base_url: str,
    timeout: float,
    headers: dict[str, str],
    transport: httpx2.AsyncBaseTransport,
) -> httpx2.AsyncClient:
    """Create the HTTP client used for one NetBox API connection.

    Redirects are followed - a NETBOX_URL that redirects (http:// to https://, a
    missing path prefix) would otherwise return the redirect page as data.
    httpx2 drops the Authorization header when a redirect leaves the origin.

    Args:
        base_url: URL every request path is relative to.
        timeout: Per-operation timeout in seconds (connect, read, write, pool).
        headers: Default headers (authorization, accept) for every request.
        transport: Transport that sends requests. It is not closed with the
            client, so it can be shared.

    Returns:
        httpx2.AsyncClient: Client with retries installed.
    """
    return httpx2.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        transport=RetryTransport(transport, owns_wrapped=False),
    )
