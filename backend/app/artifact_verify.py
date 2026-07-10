"""Artifact fetching and checksum verification with SSRF protection.

Threat: an agent registers an artifact URL pointing at ``http://169.254.169.254``
or ``http://localhost:5432`` and uses the service as a confused deputy to reach
internal networks. Defences applied here, in order:

1. Scheme allow-list (``http``, ``https`` only).
2. DNS resolution up front; every resolved address must be a global unicast
   address. Private, loopback, link-local, multicast and reserved ranges are
   rejected. (``ARTIFACT_ALLOW_PRIVATE_NETWORKS=true`` disables this for local
   development only.)
3. Redirects are not followed. A redirect is treated as a verification failure.
4. Response size capped by ``ARTIFACT_VERIFY_MAX_BYTES``; streaming stops as soon
   as the cap is exceeded.
5. Total time capped by ``ARTIFACT_VERIFY_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.errors import UnsafeArtifactUrl

ALLOWED_SCHEMES = {"http", "https"}


@dataclass
class VerificationResult:
    ok: bool
    sha256: str | None
    size_bytes: int | None
    content_type: str | None
    error: str | None = None


def _is_public_address(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeArtifactUrl(f"Could not resolve artifact host '{host}'.") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return False
    return True


def assert_safe_url(url: str) -> None:
    """Raise ``UnsafeArtifactUrl`` unless ``url`` is safe to fetch server-side."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeArtifactUrl(
            f"Unsupported URL scheme '{parsed.scheme}'. Only http and https are allowed.",
            details={"url": url},
        )
    if not parsed.hostname:
        raise UnsafeArtifactUrl("Artifact URL has no host.", details={"url": url})
    if settings.artifact_allow_private_networks:
        return
    if not _is_public_address(parsed.hostname):
        raise UnsafeArtifactUrl(
            "Artifact URL resolves to a non-public address.",
            details={"url": url, "host": parsed.hostname},
        )


def fetch_and_hash(url: str) -> VerificationResult:
    """Download ``url`` (bounded) and return its SHA-256 digest."""
    assert_safe_url(url)
    digest = hashlib.sha256()
    total = 0
    try:
        client = httpx.Client(
            timeout=settings.artifact_verify_timeout_seconds, follow_redirects=False
        )
        with client, client.stream("GET", url) as response:
                if response.is_redirect:
                    return VerificationResult(
                        False, None, None, None, "Artifact URL returned a redirect."
                    )
                if response.status_code != 200:
                    return VerificationResult(
                        False, None, None, None, f"Artifact URL returned HTTP {response.status_code}."
                    )
                content_type = response.headers.get("content-type")
                for chunk in response.iter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > settings.artifact_verify_max_bytes:
                        return VerificationResult(
                            False,
                            None,
                            None,
                            content_type,
                            f"Artifact exceeds {settings.artifact_verify_max_bytes} bytes.",
                        )
                    digest.update(chunk)
    except httpx.HTTPError as exc:
        return VerificationResult(False, None, None, None, f"Fetch failed: {type(exc).__name__}")

    return VerificationResult(True, digest.hexdigest(), total, content_type)


def verify_checksum(url: str, expected_sha256: str) -> VerificationResult:
    """Fetch ``url`` and compare its digest with ``expected_sha256``."""
    result = fetch_and_hash(url)
    if not result.ok:
        return result
    if result.sha256 != expected_sha256.lower():
        return VerificationResult(
            False,
            result.sha256,
            result.size_bytes,
            result.content_type,
            f"Checksum mismatch: expected {expected_sha256.lower()}, got {result.sha256}.",
        )
    return result
