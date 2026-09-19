"""Shared URL safety checks.

Split out of crawler.py so the same SSRF/sanity guard can run twice: once
cheaply at form-submission time (app.py, reject before a job ever enters
the queue) and once again right before the real fetch (crawler.py, the
guard that actually matters for security — app.py's check is a courtesy
that reduces wasted worker cycles on obviously-bad input, not a substitute).

The crawler-side check is now applied per *request* rather than once per
navigation (see crawler.guard_page_requests). Checking only the submitted
URL left three ways through, all of them reachable by a site that wants
to be reached from inside our network:

1. **Redirects.** Playwright follows 3xx on its own, so a public host
   could 302 to http://10.0.0.5/ and never pass this function again.
2. **Subresources.** The rendered page loads scripts, images, iframes and
   fetch() calls from wherever it likes; none of them went through here.
3. **DNS rebinding / TOCTOU.** Resolution here and the connection in
   `page.goto` are separate events, and a short-TTL record can differ
   between them. Re-resolving per request does not eliminate this (nothing
   short of pinning the resolved IP into the connection does) but it
   shrinks the window from "once per page" to "once per request".
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urlparse

# Ranges Python's own `ipaddress` does not flag but that are not the
# public internet either. Checked explicitly because `is_private` returning
# False for them is surprising and easy to assume away:
#   100.64.0.0/10  — RFC 6598 carrier-grade NAT, is_private is False.
#   192.0.0.0/24   — RFC 6890 IETF protocol assignments.
#   169.254.169.254 — the cloud metadata endpoint. Already covered by
#     is_link_local; kept explicit because it is the one address whose
#     reachability would matter most, and an explicit line documents that.
_EXTRA_REJECTED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("169.254.169.254/32"),
)


class UnsafeTargetError(Exception):
    """Raised when a URL resolves to a private/link-local/metadata address,
    or is malformed/unresolvable in a way that makes it unsafe or pointless
    to fetch.
    """


def _address_is_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or any(ip in network for network in _EXTRA_REJECTED_NETWORKS if ip.version == network.version)
    )


@lru_cache(maxsize=2048)
def _resolve(hostname: str) -> tuple[str, ...]:
    """Cached so the per-request guard does not pay a DNS round trip for
    every image on a page that loads fifty of them from one host. The cache
    is process-local and unbounded in time, which is the deliberate
    trade: within a single page load, a host that resolved safely once
    stays safe, and that is also what closes the rebinding window rather
    than widening it.
    """
    return tuple(sockaddr[0] for _, _, _, _, sockaddr in socket.getaddrinfo(hostname, None))


def assert_safe_target(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeTargetError(f"{url!r} must start with http:// or https://")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeTargetError(f"Could not parse a hostname from {url!r}")

    try:
        resolved = _resolve(hostname)
    except socket.gaierror as exc:
        raise UnsafeTargetError(f"Could not resolve {hostname!r}: {exc}") from exc

    for address in resolved:
        ip = ipaddress.ip_address(address)
        if _address_is_unsafe(ip):
            raise UnsafeTargetError(
                f"{hostname!r} resolves to {ip}, which is a private/link-local/"
                f"metadata address — refusing to fetch it."
            )


def is_safe_target(url: str) -> bool:
    """Boolean form, for the per-request guard where a raised exception
    inside a Playwright route handler would be the wrong control flow.
    Non-http(s) schemes (data:, blob:, about:) are not network requests to
    a host and are left alone — reporting them as unsafe would abort the
    inline images and blob workers a normal page legitimately uses.
    """
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        return True
    try:
        assert_safe_target(url)
    except UnsafeTargetError:
        return False
    return True


# ---- Running these checks from async code, without poisoning the process.
#
# `socket.getaddrinfo` is a blocking C call with no timeout parameter, and
# `socket.setdefaulttimeout` does not reach it. `asyncio.wait_for` around
# an `asyncio.to_thread` therefore bounds the *response*, not the work:
# cancelling the awaitable abandons the coroutine while the resolution runs
# to completion in whatever executor it landed in.
#
# That is tolerable in itself. What was not tolerable is where it landed:
# the default executor, capped at `min(32, cpu_count + 4)` threads and
# shared with every other `asyncio.to_thread` in the process -- including
# `editor.verify_findings`'s synchronous embedding call on the worker.
# Under a burst of submissions naming hosts with slow or blackholed DNS,
# abandoned resolutions accumulate there and, once it saturates, *every*
# to_thread on that instance queues behind them. A slow-DNS attack that
# should have degraded one request degraded the whole instance.
#
# A dedicated, bounded pool contains that: a saturated DNS pool now slows
# only DNS. It does not make the thread cancellable -- nothing short of a
# resolver library that owns its own sockets would -- and the caller's
# `wait_for` is still what gives the visitor an answer. The honest summary
# is that the timeout is a response deadline and the pool is the blast
# radius, and the two are now separate things rather than one thing
# pretending to be both.
#
# 8 workers: enough that a page pulling subresources from a handful of
# hosts never queues in practice (the lru_cache above means one lookup per
# host, not per request), small enough to be a meaningful ceiling.
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mad-dns")


async def assert_safe_target_async(url: str, timeout: float | None = None) -> None:
    """`assert_safe_target` off the event loop, on the DNS pool.

    Raises `UnsafeTargetError` as the sync version does, and
    `asyncio.TimeoutError` if `timeout` elapses first -- the caller decides
    what to show for each, since they mean different things to a visitor.
    """
    loop = asyncio.get_running_loop()
    call = loop.run_in_executor(_DNS_EXECUTOR, assert_safe_target, url)
    if timeout is None:
        await call
        return
    await asyncio.wait_for(call, timeout=timeout)


async def is_safe_target_async(url: str) -> bool:
    """Boolean form, for the per-request Playwright guard."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DNS_EXECUTOR, is_safe_target, url)


def dns_executor() -> ThreadPoolExecutor:
    """The shared bounded pool above, for other callers with their own
    blocking `getaddrinfo` -- abuse_guard.email_looks_valid is the other
    one in this codebase. The point of the pool is the shared ceiling, so
    a second blocking-DNS caller belongs on this one, not a pool of its
    own (which would just relocate the saturation risk) or the process
    default executor (which is the thing this was built to get off of).
    """
    return _DNS_EXECUTOR
