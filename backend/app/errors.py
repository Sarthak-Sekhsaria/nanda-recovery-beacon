"""Machine-readable error envelope.

Every non-2xx response produced by this service has the shape::

    {
      "error": {
        "code": "CLAIM_ALREADY_HELD",
        "message": "This workflow already has an active claim.",
        "retryable": true,
        "retry_after_seconds": 42,
        "details": {}
      },
      "request_id": "01J..."
    }

Agents should branch on ``error.code``, never on the message text.
"""

from __future__ import annotations

from typing import Any


class BeaconError(Exception):
    """Base class for every error the API returns deliberately."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred."
    retryable: bool = False

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        retry_after_seconds: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details or {}
        self.retry_after_seconds = retry_after_seconds
        if retryable is not None:
            self.retryable = retryable

    def to_body(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "retry_after_seconds": self.retry_after_seconds,
                "details": self.details,
            },
            "request_id": request_id,
        }


# --- 400 ---------------------------------------------------------------------
class BadRequest(BeaconError):
    status_code = 400
    code = "BAD_REQUEST"
    message = "The request could not be parsed."


class RequestTooLarge(BeaconError):
    status_code = 413
    code = "REQUEST_TOO_LARGE"
    message = "Request body exceeds the configured maximum size."


# --- 401 / 403 ---------------------------------------------------------------
class Unauthenticated(BeaconError):
    status_code = 401
    code = "UNAUTHENTICATED"
    message = "Missing or invalid API key. Send 'Authorization: Bearer <api_key>'."


class Forbidden(BeaconError):
    status_code = 403
    code = "FORBIDDEN"
    message = "This agent is not permitted to perform that action."


class NotLeaseHolder(BeaconError):
    status_code = 403
    code = "NOT_LEASE_HOLDER"
    message = "An active claim exists and you are not its holder. Claim the workflow first."


class AdminRequired(BeaconError):
    status_code = 403
    code = "ADMIN_REQUIRED"
    message = "This endpoint requires an admin API key."


# --- 404 ---------------------------------------------------------------------
class NotFound(BeaconError):
    status_code = 404
    code = "NOT_FOUND"
    message = "The requested resource does not exist."


class WorkflowNotFound(NotFound):
    code = "WORKFLOW_NOT_FOUND"
    message = "No workflow exists with that id."


class CheckpointNotFound(NotFound):
    code = "CHECKPOINT_NOT_FOUND"
    message = "No checkpoint exists with that version."


class ClaimNotFound(NotFound):
    code = "CLAIM_NOT_FOUND"
    message = "This workflow has no active claim."


class ArtifactNotFound(NotFound):
    code = "ARTIFACT_NOT_FOUND"
    message = "No artifact exists with that id."


# --- 409 ---------------------------------------------------------------------
class Conflict(BeaconError):
    status_code = 409
    code = "CONFLICT"
    message = "The request conflicts with the current state of the resource."


class ClaimAlreadyHeld(Conflict):
    code = "CLAIM_ALREADY_HELD"
    message = "This workflow already has an active claim."
    retryable = True


class WorkflowNotRecoverable(Conflict):
    code = "WORKFLOW_NOT_RECOVERABLE"
    message = "This workflow is not in a claimable state."


class InvalidStateTransition(Conflict):
    code = "INVALID_STATE_TRANSITION"
    message = "That transition is not allowed from the workflow's current status."


class StaleCheckpointVersion(Conflict):
    code = "STALE_CHECKPOINT_VERSION"
    message = "parent_version does not match the workflow's current checkpoint version."


class FencingTokenStale(Conflict):
    code = "FENCING_TOKEN_STALE"
    message = "Your lease has been superseded by a newer claim generation."


class IdempotencyKeyReused(Conflict):
    code = "IDEMPOTENCY_KEY_REUSED"
    message = "This Idempotency-Key was already used with a different request body."


class WorkflowAlreadyCompleted(Conflict):
    code = "WORKFLOW_ALREADY_COMPLETED"
    message = "This workflow is already completed. Completion is not repeatable."


# --- 410 ---------------------------------------------------------------------
class LeaseExpired(BeaconError):
    status_code = 410
    code = "LEASE_EXPIRED"
    message = "Your lease expired. Re-claim the workflow before submitting progress."
    retryable = True


# --- 422 ---------------------------------------------------------------------
class DomainValidationError(BeaconError):
    status_code = 422
    code = "DOMAIN_VALIDATION_FAILED"
    message = "The request is valid JSON but violates a domain rule."


class SchemaValidationError(BeaconError):
    status_code = 422
    code = "SCHEMA_VALIDATION_FAILED"
    message = "The request body failed schema validation."


class CompletionRequirementsNotMet(BeaconError):
    status_code = 422
    code = "COMPLETION_REQUIREMENTS_NOT_MET"
    message = "The workflow does not satisfy every completion requirement."


class UnsupportedSchemaVersion(BeaconError):
    status_code = 422
    code = "UNSUPPORTED_SCHEMA_VERSION"
    message = "That checkpoint schema_version is not supported by this service."


class ArtifactVerificationFailed(BeaconError):
    status_code = 422
    code = "ARTIFACT_VERIFICATION_FAILED"
    message = "The artifact could not be fetched or its checksum did not match."


class UnsafeArtifactUrl(BeaconError):
    status_code = 422
    code = "UNSAFE_ARTIFACT_URL"
    message = "Artifact URLs must be http(s) and must not resolve to a private network."


class StorageBackendDisabled(BeaconError):
    status_code = 501
    code = "STORAGE_BACKEND_DISABLED"
    message = "Direct artifact upload is disabled. Set STORAGE_BACKEND=s3 to enable it."


# --- 429 / 503 ---------------------------------------------------------------
class RateLimited(BeaconError):
    status_code = 429
    code = "RATE_LIMITED"
    message = "Too many requests. Slow down and retry after the indicated delay."
    retryable = True


class ServiceUnavailable(BeaconError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"
    message = "The service is temporarily unable to handle the request."
    retryable = True
