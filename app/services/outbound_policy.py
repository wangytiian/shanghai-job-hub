"""Validate server-side HTTP destinations before outbound calls in cloud mode."""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse


class OutboundPolicyError(ValueError):
    pass


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def validate_public_http_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
) -> str:
    """Return a normalized public HTTP(S) URL or reject unsafe destinations.

    DNS is resolved before connection so a hostname cannot be used to reach a
    private network or cloud metadata endpoint. Call this immediately before a
    request; redirect targets must be validated again by their caller.
    """
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OutboundPolicyError("外部地址必须是完整的 http 或 https 地址")
    if parsed.username or parsed.password:
        raise OutboundPolicyError("外部地址不能包含账号或密码")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = resolver(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundPolicyError("外部地址无法解析") from exc
    if not addresses:
        raise OutboundPolicyError("外部地址无法解析")
    for record in addresses:
        resolved_ip = record[4][0]
        if not _is_public_ip(resolved_ip):
            raise OutboundPolicyError("外部地址解析到私网、回环或保留地址")
    return cleaned
