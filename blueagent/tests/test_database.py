"""Tests for DatabaseManager SQLite relational persistence (v2 schema)."""

import pytest
import sqlite3
from blueagent.database import DatabaseManager, StateConflictError


@pytest.fixture
async def db_mgr(tmp_path):
    db_file = tmp_path / "test_blueagent.db"
    mgr = DatabaseManager(db_path=str(db_file))
    await mgr.init_db()
    return mgr


@pytest.mark.asyncio
async def test_team_crud(db_mgr):
    # 1. Create Team
    team = await db_mgr.create_team(
        team_k=1,
        name="Team Red",
        bridge_name="br-team-1",
        wg_interface="wg-1",
    )
    assert team.team_k == 1
    assert team.name == "Team Red"
    assert team.bridge_name == "br-team-1"
    assert team.wg_interface == "wg-1"

    # 2. Get Team
    fetched = await db_mgr.get_team_by_k(1)
    assert fetched is not None
    assert fetched.team_k == team.team_k
    assert fetched.name == "Team Red"

    # 3. List Teams
    teams = await db_mgr.list_teams()
    assert len(teams) == 1
    assert teams[0].team_k == 1

    # 4. Delete Team
    deleted = await db_mgr.delete_team(1)
    assert deleted is True

    # 5. Verify Not Found
    assert await db_mgr.get_team_by_k(1) is None
    assert len(await db_mgr.list_teams()) == 0


@pytest.mark.asyncio
async def test_team_members_and_cascade(db_mgr):
    team = await db_mgr.create_team(
        team_k=2,
        name="Team Blue",
        bridge_name="br-team-2",
        wg_interface="wg-2",
    )

    m1 = await db_mgr.add_team_member(
        member_id="m-alice",
        team_k=2,
        name="Alice",
        client_ip="10.2.0.2/32",
        public_key="pubkey_alice_123",
        private_key="privkey_alice_123",
        config_path="/etc/wireguard/teams/2/m-alice.conf",
    )
    assert m1.member_id == "m-alice"

    m2 = await db_mgr.add_team_member(
        member_id="m-bob",
        team_k=2,
        name="Bob",
        client_ip="10.2.0.3/32",
        public_key="pubkey_bob_456",
    )
    assert m2.member_id == "m-bob"

    members = await db_mgr.list_team_members(2)
    assert len(members) == 2

    # Cascade delete on team delete
    await db_mgr.delete_team(2)
    assert await db_mgr.get_team_member("m-alice") is None
    assert len(await db_mgr.list_team_members(2)) == 0


@pytest.mark.asyncio
async def test_images_crud_and_cas(db_mgr):
    # Claim new image
    rec, needs_build = await db_mgr.claim_image(
        alias="debian-vuln",
        yaml_sha256="sha-1234",
        yaml_text="image: debian",
    )
    assert rec.alias == "debian-vuln"
    assert rec.status == "building"
    assert needs_build is True

    # Re-claim while building returns existing
    rec2, needs_build2 = await db_mgr.claim_image(
        alias="debian-vuln",
        yaml_sha256="sha-1234",
        yaml_text="image: debian",
    )
    assert rec2.status == "building"
    assert needs_build2 is False

    # Attempting to reuse alias for different YAML sha raises StateConflictError
    with pytest.raises(StateConflictError):
        await db_mgr.claim_image(
            alias="debian-vuln",
            yaml_sha256="sha-different",
            yaml_text="image: ubuntu",
        )

    # CAS to ready
    ok = await db_mgr.set_image_status(
        alias="debian-vuln",
        new_status="ready",
        expected_statuses=["building"],
        fingerprint="fp-abcdef",
    )
    assert ok is True

    # Re-claiming ready image is cache hit
    rec3, needs_build3 = await db_mgr.claim_image(
        alias="debian-vuln",
        yaml_sha256="sha-1234",
        yaml_text="image: debian",
    )
    assert rec3.status == "ready"
    assert rec3.fingerprint == "fp-abcdef"
    assert needs_build3 is False

    # CAS mismatch returns False
    ok_bad = await db_mgr.set_image_status(
        alias="debian-vuln",
        new_status="failed",
        expected_statuses=["building"],
    )
    assert ok_bad is False

    # Get image
    img = await db_mgr.get_image("debian-vuln")
    assert img is not None
    assert img.fingerprint == "fp-abcdef"

    # Delete image
    assert await db_mgr.delete_image("debian-vuln") is True
    assert await db_mgr.get_image("debian-vuln") is None


@pytest.mark.asyncio
async def test_running_containers_lifecycle(db_mgr):
    # Setup team and image
    await db_mgr.create_team(team_k=3, name="Team 3", bridge_name="br-team-3", wg_interface="wg-3")
    await db_mgr.claim_image(alias="img-web", yaml_sha256="sha-web", yaml_text="yaml")
    await db_mgr.set_image_status("img-web", "ready", ["building"], fingerprint="fp-web")

    # Record container 1
    c1 = await db_mgr.record_container(
        instance_name="c-3-web",
        team_k=3,
        spec_name="web",
        ip_address="10.3.1.2",
        image_alias="img-web",
    )
    assert c1.instance_name == "c-3-web"
    assert c1.team_k == 3

    # Record container 2
    c2 = await db_mgr.record_container(
        instance_name="c-3-db",
        team_k=3,
        spec_name="db",
        ip_address="10.3.1.3",
        image_alias="img-web",
    )
    assert c2.instance_name == "c-3-db"

    # Get container
    fetched = await db_mgr.get_container("c-3-web")
    assert fetched is not None
    assert fetched.spec_name == "web"

    # Get container by spec
    by_spec = await db_mgr.get_container_by_spec(3, "db")
    assert by_spec is not None
    assert by_spec.instance_name == "c-3-db"

    # List containers for team
    cnts = await db_mgr.list_team_containers(3)
    assert len(cnts) == 2

    # IP uniqueness per team check: duplicate IP raises IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        await db_mgr.record_container(
            instance_name="c-3-dup-ip",
            team_k=3,
            spec_name="other",
            ip_address="10.3.1.2",
            image_alias="img-web",
        )

    # Foreign key RESTRICT: cannot delete image when in-use
    with pytest.raises(sqlite3.IntegrityError):
        await db_mgr.delete_image("img-web")

    # Delete single container
    assert await db_mgr.delete_container("c-3-web") is True
    assert await db_mgr.get_container("c-3-web") is None
    assert len(await db_mgr.list_team_containers(3)) == 1

    # Delete all team containers
    deleted_count = await db_mgr.delete_all_team_containers(3)
    assert deleted_count == 1
    assert len(await db_mgr.list_team_containers(3)) == 0

    # Now image and team can be deleted
    assert await db_mgr.delete_image("img-web") is True
    assert await db_mgr.delete_team(3) is True


@pytest.mark.asyncio
async def test_startup_reconciliation(db_mgr):
    await db_mgr.claim_image(alias="img-interrupted", yaml_sha256="sha-int", yaml_text="yaml")

    # In flight: image is 'building'
    res = await db_mgr.reconcile_startup_states()
    assert res["images_reconciled"] == 1

    img = await db_mgr.get_image("img-interrupted")
    assert img.status == "failed"
    assert "Interrupted" in img.error
