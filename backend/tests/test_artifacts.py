"""Artifact registration, SHA-256 verification and SSRF protection."""

from __future__ import annotations

import hashlib
import http.server
import socket
import threading
from collections.abc import Iterator

import pytest

from app.artifact_verify import assert_safe_url, verify_checksum
from app.config import settings
from app.errors import UnsafeArtifactUrl
from tests.conftest import create_workflow, error_code

PAYLOAD = b'{"programs": [1, 2, 3, 4, 5]}'
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/programs.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.com/elsewhere")
            self.end_headers()
        elif self.path == "/huge":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * (settings.artifact_verify_max_bytes + 1024))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args) -> None:  # silence the stdlib access log
        pass


@pytest.fixture(scope="module")
def artifact_server() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def test_registering_an_artifact_with_a_matching_checksum_verifies_it(client, artifact_server):
    workflow = create_workflow(client)
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/artifacts",
        json={
            "name": "programs.json",
            "uri": f"{artifact_server}/programs.json",
            "sha256": PAYLOAD_SHA256,
            "verify": True,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["verification_status"] == "verified"
    assert body["size_bytes"] == len(PAYLOAD)
    assert body["verified_at"] is not None


def test_a_checksum_mismatch_is_422_and_records_the_failure(client, artifact_server):
    workflow = create_workflow(client)
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/artifacts",
        json={
            "name": "programs.json",
            "uri": f"{artifact_server}/programs.json",
            "sha256": "0" * 64,
            "verify": True,
        },
    )

    assert response.status_code == 422
    assert error_code(response) == "ARTIFACT_VERIFICATION_FAILED"

    artifacts = client.get(f"/api/v1/workflows/{workflow['id']}/artifacts").json()["items"]
    assert artifacts[0]["verification_status"] == "failed"
    assert "mismatch" in artifacts[0]["verification_error"].lower()

    events = client.get(f"/api/v1/workflows/{workflow['id']}/events").json()["items"]
    assert "artifact_verification_failed" in [e["event_type"] for e in events]


def test_a_failed_artifact_blocks_completion(client, artifact_server):
    from tests.conftest import GOOD_CHECKPOINT

    workflow = create_workflow(client)
    wid = workflow["id"]
    client.post(
        f"/api/v1/workflows/{wid}/artifacts",
        json={
            "name": "bad.json",
            "uri": f"{artifact_server}/programs.json",
            "sha256": "1" * 64,
            "verify": True,
        },
    )
    client.post(
        f"/api/v1/workflows/{wid}/checkpoints",
        json=dict(GOOD_CHECKPOINT, parent_version=1, remaining_steps=[], next_action="done"),
    )

    response = client.post(f"/api/v1/workflows/{wid}/complete", json={"final_checkpoint_version": 2})
    assert response.status_code == 422
    unmet = [r["requirement"] for r in response.json()["error"]["details"]["unmet_requirements"]]
    assert "NO_FAILED_ARTIFACTS" in unmet


def test_re_verifying_a_good_artifact_succeeds(client, artifact_server):
    workflow = create_workflow(client)
    created = client.post(
        f"/api/v1/workflows/{workflow['id']}/artifacts",
        json={
            "name": "programs.json",
            "uri": f"{artifact_server}/programs.json",
            "sha256": PAYLOAD_SHA256,
        },
    ).json()
    assert created["verification_status"] == "unverified"

    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/artifacts/{created['id']}/verify", json={}
    )
    assert response.status_code == 200
    assert response.json()["verification_status"] == "verified"


def test_artifact_without_a_location_is_rejected(client):
    workflow = create_workflow(client)
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/artifacts", json={"name": "nowhere.json"}
    )
    assert response.status_code == 422
    assert error_code(response) == "SCHEMA_VALIDATION_FAILED"


def test_a_redirect_fails_verification(client, artifact_server):
    result = verify_checksum(f"{artifact_server}/redirect", PAYLOAD_SHA256)
    assert result.ok is False
    assert "redirect" in result.error.lower()


def test_an_oversized_artifact_fails_verification(client, artifact_server):
    result = verify_checksum(f"{artifact_server}/huge", PAYLOAD_SHA256)
    assert result.ok is False
    assert "exceeds" in result.error.lower()


def test_ssrf_guard_rejects_non_http_schemes():
    with pytest.raises(UnsafeArtifactUrl) as excinfo:
        assert_safe_url("file:///etc/passwd")
    assert excinfo.value.code == "UNSAFE_ARTIFACT_URL"


def test_ssrf_guard_rejects_private_addresses(monkeypatch):
    monkeypatch.setattr(settings, "artifact_allow_private_networks", False)

    for url in (
        "http://127.0.0.1:5432/",
        "http://localhost/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
    ):
        with pytest.raises(UnsafeArtifactUrl):
            assert_safe_url(url)


def test_ssrf_guard_allows_public_hosts(monkeypatch):
    monkeypatch.setattr(settings, "artifact_allow_private_networks", False)
    assert_safe_url("https://example.com/programs.json")  # must not raise


def test_upload_url_is_501_when_storage_is_disabled(client):
    workflow = create_workflow(client)
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/artifacts/upload-url", json={"name": "out.bin"}
    )
    assert response.status_code == 501
    assert error_code(response) == "STORAGE_BACKEND_DISABLED"
