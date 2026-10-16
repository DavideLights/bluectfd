"""Usability Test 2: Image Creation and Catalog Management.

Validates:
- Image creation: Submitting Distrobuilder YAML via multipart POST /images.
- Async compilation pipeline: Distrobuilder execution, tarball generation, Incus import, and alias creation.
- Polling for readiness: Verifying CAS status progression from 'building' to 'ready'.
- Runtime registration: Verifying image presence in both Incus daemon and relational database.
- Idempotency and duplicate submission handling.
- Input validation: Ensuring bad YAML or invalid alias patterns are rejected.
"""

import subprocess
import pytest
from pathlib import Path
from .client import BlueAgentTestClient

IMAGE_ALIAS = "alpine-distro"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
YAML_PATH = REPO_ROOT / "alpine-3.20.yaml"


@pytest.mark.asyncio
async def test_image_invalid_inputs_rejected(client: BlueAgentTestClient, tmp_path: Path):
    """Verifies malformed YAML and invalid alias naming conventions are rejected with HTTP 422."""
    # 1. Invalid YAML content
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("invalid: [broken: {yaml", encoding="utf-8")
    resp = await client.create_image("valid-alias", bad_yaml)
    assert resp.status_code == 422, f"Expected 422 for invalid YAML, got {resp.status_code}"

    # 2. Invalid alias pattern (capital letters / illegal symbols)
    valid_yaml = tmp_path / "valid.yaml"
    valid_yaml.write_text("image:\n  distribution: alpine\n", encoding="utf-8")
    resp2 = await client.create_image("INVALID_ALIAS_NAME!", valid_yaml)
    assert resp2.status_code == 422, f"Expected 422 for invalid alias pattern, got {resp2.status_code}"


@pytest.mark.asyncio
async def test_image_build_pipeline_and_incus_import(client: BlueAgentTestClient):
    """Test full Distrobuilder compile, Incus import, and status tracking to ready."""
    assert YAML_PATH.exists(), f"{YAML_PATH} must exist for this test"

    # Delete existing test image if present
    await client.delete_image(IMAGE_ALIAS)

    # 1. Submit Distrobuilder YAML
    resp = await client.create_image(IMAGE_ALIAS, YAML_PATH)
    assert resp.status_code in (200, 202), f"Expected 200 or 202, got {resp.status_code}: {resp.text}"
    record = resp.json()
    assert record["alias"] == IMAGE_ALIAS
    assert record["status"] in ("building", "ready")

    # 2. Poll until image is ready (timeout: 180s)
    ready_data = await client.wait_for_image_ready(IMAGE_ALIAS, timeout=180.0, poll_interval=3.0)
    assert ready_data["status"] == "ready", f"Unexpected status: {ready_data}"
    assert ready_data.get("fingerprint"), "Expected valid SHA256 fingerprint in image record"
    fingerprint = ready_data["fingerprint"]

    # 3. Verify in Incus runtime
    incus_proc = subprocess.run(
        ["incus", "image", "list", "--format=json"],
        capture_output=True,
        text=True,
    )
    assert incus_proc.returncode == 0, f"incus image list failed: {incus_proc.stderr}"
    assert IMAGE_ALIAS in incus_proc.stdout, f"Alias {IMAGE_ALIAS} not found in Incus image list"


@pytest.mark.asyncio
async def test_image_listing_and_retrieval(client: BlueAgentTestClient):
    """Verifies GET /images and GET /images/{alias} return accurate catalog metadata."""
    # Retrieve specific image
    resp = await client.get_image(IMAGE_ALIAS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["alias"] == IMAGE_ALIAS
    assert data["status"] == "ready"

    # List images
    list_resp = await client.list_images()
    assert list_resp.status_code == 200
    images = list_resp.json()
    assert any(img["alias"] == IMAGE_ALIAS for img in images)


@pytest.mark.asyncio
async def test_image_duplicate_submission_is_idempotent(client: BlueAgentTestClient):
    """Submitting the same YAML for an existing ready image should return 200 without rebuilding."""
    resp = await client.create_image(IMAGE_ALIAS, YAML_PATH)
    assert resp.status_code == 200, f"Expected 200 for already ready image, got {resp.status_code}"
    data = resp.json()
    assert data["alias"] == IMAGE_ALIAS
    assert data["status"] == "ready"
