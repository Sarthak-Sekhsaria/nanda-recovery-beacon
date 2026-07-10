"""Artifact storage abstraction.

The service ships with two backends:

* ``none`` (default) - artifacts are referenced by an externally hosted URL plus a
  SHA-256 checksum. Nothing is uploaded through the Beacon.
* ``s3`` - any S3-compatible object store (AWS S3, Cloudflare R2, MinIO). The
  Beacon hands out pre-signed PUT/GET URLs; artifact bytes never pass through it.

Selecting ``s3`` requires ``pip install '.[s3]'`` and the S3_* environment
variables. When the backend is ``none``, upload endpoints answer 501 with
``STORAGE_BACKEND_DISABLED`` rather than pretending to work.
"""

from __future__ import annotations

from typing import Protocol

from app.config import settings
from app.errors import ServiceUnavailable, StorageBackendDisabled


class StorageBackend(Protocol):
    enabled: bool

    def presigned_put(self, key: str, content_type: str | None = None) -> dict: ...

    def presigned_get(self, key: str) -> str: ...


class NullStorage:
    """External-URL-only mode."""

    enabled = False

    def presigned_put(self, key: str, content_type: str | None = None) -> dict:
        raise StorageBackendDisabled()

    def presigned_get(self, key: str) -> str:
        raise StorageBackendDisabled()


class S3Storage:
    enabled = True

    def __init__(self) -> None:
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ServiceUnavailable(
                "STORAGE_BACKEND=s3 but boto3 is not installed. Install the 's3' extra."
            ) from exc
        if not settings.s3_bucket:
            raise ServiceUnavailable("STORAGE_BACKEND=s3 requires S3_BUCKET.")
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            region_name=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
        )

    def presigned_put(self, key: str, content_type: str | None = None) -> dict:
        params: dict = {"Bucket": self._bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        url = self._client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=settings.s3_presign_expiry_seconds
        )
        return {
            "upload_url": url,
            "method": "PUT",
            "storage_key": key,
            "expires_in_seconds": settings.s3_presign_expiry_seconds,
        }

    def presigned_get(self, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=settings.s3_presign_expiry_seconds,
        )


_backend: StorageBackend | None = None


def get_storage() -> StorageBackend:
    global _backend
    if _backend is None:
        _backend = S3Storage() if settings.storage_backend.lower() == "s3" else NullStorage()
    return _backend
