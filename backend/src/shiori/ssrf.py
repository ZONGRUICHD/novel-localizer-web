from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .errors import ErrorCode, ShioriError


@dataclass(frozen=True)
class ValidatedEndpoint:
    base_url: str
    hostname: str
    addresses: tuple[str, ...]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(ip.is_global) and not any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


async def validate_public_https_base_url(value: str) -> ValidatedEndpoint:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ShioriError(
            ErrorCode.API_INCOMPATIBLE,
            "Base URL must be an absolute public HTTPS URL",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ShioriError(
            ErrorCode.API_INCOMPATIBLE,
            "Base URL cannot contain credentials, a query, or a fragment",
        )
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise ShioriError(ErrorCode.API_INCOMPATIBLE, "Local provider hosts are not allowed")

    addresses: tuple[str, ...]
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (str(literal),)
    else:
        try:
            infos = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: socket.getaddrinfo(
                    hostname,
                    parsed.port or 443,
                    type=socket.SOCK_STREAM,
                ),
            )
        except socket.gaierror as exc:
            raise ShioriError(
                ErrorCode.PROVIDER_UNREACHABLE,
                "Provider host could not be resolved",
                status_code=422,
            ) from exc
        addresses = tuple(sorted({str(info[4][0]) for info in infos}))

    if not addresses or any(not _is_public(address) for address in addresses):
        raise ShioriError(
            ErrorCode.API_INCOMPATIBLE,
            "Provider host resolves to a non-public address",
        )

    netloc = hostname
    if ":" in hostname:
        netloc = f"[{hostname}]"
    if parsed.port and parsed.port != 443:
        netloc = f"{netloc}:{parsed.port}"
    normalized_path = parsed.path.rstrip("/")
    normalized = urlunsplit(("https", netloc, normalized_path, "", ""))
    return ValidatedEndpoint(normalized, hostname, addresses)


def provider_endpoint(base_url: str, suffix: str) -> str:
    """Build a standard OpenAI v1 endpoint without silently duplicating /v1."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/{suffix.lstrip('/')}"
    return f"{normalized}/v1/{suffix.lstrip('/')}"
