"""Shared URL safety checks.

Split out of crawler.py so the same SSRF/sanity guard can run twice: once
cheaply at form-submission time (app.py, reject before a job ever enters
the queue) and once again right before the real fetch (crawler.py, the
guard that actually matters for security — app.py's check is a courtesy
that reduces wasted worker cycles on obviously-bad input, not a substitute).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeTargetError(Exception):
    """Raised when a URL resolves to a private/link-local/metadata address,
    or is malformed/unresolvable in a way that makes it unsafe or pointless
    to fetch.
    """


def assert_safe_target(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeTargetError(f"{url!r} must start with http:// or https://")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeTargetError(f"Could not parse a hostname from {url!r}")

    try:
        resolved = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeTargetError(f"Could not resolve {hostname!r}: {exc}") from exc

    for family, _, _, _, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or str(ip) == "169.254.169.254"  # cloud metadata endpoint, explicit belt-and-suspenders
        ):
            raise UnsafeTargetError(
                f"{hostname!r} resolves to {ip}, which is a private/link-local/"
                f"metadata address — refusing to fetch it."
            )
