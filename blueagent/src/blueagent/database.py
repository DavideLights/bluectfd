"""Relational state management for BlueAgent v2 using SQLite (aiosqlite).

Provides ACID persistence for:
1. Team allocations, bridge designations, and WireGuard interfaces.
2. Multi-member WireGuard peer configurations and cryptokey assignments.
3. Distrobuilder container images catalog and build lifecycle.
4. Active challenge deployments, instance allocations, and tracking.

Utilizes WAL (Write-Ahead Logging) and consistent connection configuration.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import aiosqlite

from .config import settings
from .models import (
    ImageRecord,
    RunningContainerRecord,
    TeamMemberWG,
    TeamRecord,
)


class DatabaseError(Exception):
    """Base exception for database failures."""


class StateConflictError(DatabaseError):
    """Raised when an operation violates state transition invariants or concurrency checks."""


class ResourceNotFoundError(DatabaseError):
    """Raised when a requested resource does not exist."""


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS teams (
    team_k INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    bridge_name TEXT NOT NULL,
    wg_interface TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_members (
    member_id TEXT PRIMARY KEY,
    team_k INTEGER NOT NULL,
    ctfd_user_id INTEGER UNIQUE,
    name TEXT NOT NULL,
    client_ip TEXT NOT NULL,
    public_key TEXT NOT NULL,
    private_key TEXT,
    preshared_key TEXT,
    config_path TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (team_k) REFERENCES teams(team_k) ON DELETE CASCADE,
    UNIQUE(team_k, client_ip)
);

CREATE TABLE IF NOT EXISTS images (
    alias TEXT PRIMARY KEY,
    yaml_sha256 TEXT NOT NULL,
    yaml_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    fingerprint TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS running_containers (
    instance_name TEXT PRIMARY KEY,
    team_k INTEGER NOT NULL REFERENCES teams(team_k) ON DELETE CASCADE,
    spec_name TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    image_alias TEXT NOT NULL REFERENCES images(alias) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (team_k, spec_name),
    UNIQUE (team_k, ip_address)
);

CREATE INDEX IF NOT EXISTS idx_teams_team_k ON teams(team_k);
CREATE INDEX IF NOT EXISTS idx_team_members_team_k ON team_members(team_k);
CREATE INDEX IF NOT EXISTS idx_team_members_ctfd_user_id ON team_members(ctfd_user_id);
CREATE INDEX IF NOT EXISTS idx_running_containers_team_k ON running_containers(team_k);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
"""


class DatabaseManager:
    """Asynchronous database manager encapsulating SQLite connection and queries."""

    def __init__(self, db_path: str = settings.database_path):
        self.db_path = db_path

    @asynccontextmanager
    async def _connect(self):
        """Helper to create and configure a database connection."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode = WAL;")
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute("PRAGMA busy_timeout = 5000;")
            yield conn

    async def get_connection(self) -> aiosqlite.Connection:
        """Returns an active SQLite connection configured with WAL and row factory."""
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    async def init_db(self) -> None:
        """Initializes database tables, indices, and schema version."""
        db_file = Path(self.db_path)
        if db_file.parent and not db_file.parent.exists():
            db_file.parent.mkdir(parents=True, exist_ok=True)

        async with self._connect() as db:
            await db.executescript(SCHEMA_SQL)
            await db.commit()

    # --- Team CRUD Operations ---

    async def create_team(
        self,
        team_k: int,
        name: str,
        bridge_name: str,
        wg_interface: str,
    ) -> TeamRecord:
        """Inserts a new team record."""
        now_str = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO teams (team_k, name, bridge_name, wg_interface, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (team_k, name, bridge_name, wg_interface, now_str),
            )
            await db.commit()
            return TeamRecord(
                team_k=team_k,
                name=name,
                bridge_name=bridge_name,
                wg_interface=wg_interface,
                created_at=datetime.fromisoformat(now_str),
            )

    async def update_team_name(self, team_k: int, name: str) -> Optional[TeamRecord]:
        """Updates team name."""
        async with self._connect() as db:
            await db.execute("UPDATE teams SET name = ? WHERE team_k = ?", (name, team_k))
            await db.commit()
        return await self.get_team_by_k(team_k)

    async def get_team_by_k(self, team_k: int) -> Optional[TeamRecord]:
        """Retrieves a team by its index k."""
        async with self._connect() as db:
            async with db.execute("SELECT * FROM teams WHERE team_k = ?", (team_k,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return TeamRecord(
                    team_k=row["team_k"],
                    name=row["name"],
                    bridge_name=row["bridge_name"],
                    wg_interface=row["wg_interface"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )

    async def list_teams(self) -> List[TeamRecord]:
        """Lists all registered teams."""
        async with self._connect() as db:
            async with db.execute("SELECT * FROM teams ORDER BY team_k ASC") as cursor:
                rows = await cursor.fetchall()
                return [
                    TeamRecord(
                        team_k=row["team_k"],
                        name=row["name"],
                        bridge_name=row["bridge_name"],
                        wg_interface=row["wg_interface"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                    for row in rows
                ]

    async def delete_team(self, team_k: int) -> bool:
        """Deletes a team and cascades deletion to associated members and containers."""
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM teams WHERE team_k = ?", (team_k,))
            await db.commit()
            return cursor.rowcount > 0

    # --- Team Member WireGuard CRUD Operations ---

    async def add_team_member(
        self,
        member_id: str,
        team_k: int,
        name: str,
        client_ip: str,
        public_key: str,
        ctfd_user_id: Optional[int] = None,
        private_key: Optional[str] = None,
        preshared_key: Optional[str] = None,
        config_path: Optional[str] = None,
    ) -> TeamMemberWG:
        """Records a new team member and their allocated WireGuard credentials."""
        now_str = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO team_members (
                    member_id, team_k, ctfd_user_id, name, client_ip, public_key,
                    private_key, preshared_key, config_path, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member_id,
                    team_k,
                    ctfd_user_id,
                    name,
                    client_ip,
                    public_key,
                    private_key,
                    preshared_key,
                    config_path,
                    now_str,
                ),
            )
            await db.commit()
            return TeamMemberWG(
                member_id=member_id,
                team_k=team_k,
                ctfd_user_id=ctfd_user_id,
                name=name,
                client_ip=client_ip,
                public_key=public_key,
                private_key=private_key,
                preshared_key=preshared_key,
                config_path=config_path,
                created_at=datetime.fromisoformat(now_str),
            )

    async def update_team_member(
        self,
        member_id: str,
        name: Optional[str] = None,
        public_key: Optional[str] = None,
        private_key: Optional[str] = None,
    ) -> Optional[TeamMemberWG]:
        """Updates mutable fields for a team member."""
        updates: list[str] = []
        params: list[Any] = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if public_key is not None:
            updates.append("public_key = ?")
            params.append(public_key)
        if private_key is not None:
            updates.append("private_key = ?")
            params.append(private_key)
        elif public_key is not None:
            updates.append("private_key = NULL")

        if not updates:
            return await self.get_team_member(member_id)

        params.append(member_id)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE team_members SET {', '.join(updates)} WHERE member_id = ?",
                params,
            )
            await db.commit()
        return await self.get_team_member(member_id)

    async def get_team_member(self, member_id: str, team_k: Optional[int] = None) -> Optional[TeamMemberWG]:
        """Retrieves an individual team member by unique identifier or user_id."""
        user_int = int(member_id) if str(member_id).isdigit() else None
        alt_id = f"ctfd-{team_k}-{member_id}" if team_k is not None else member_id
        async with self._connect() as db:
            query = """
                SELECT *
                FROM team_members
                WHERE (member_id = ? OR member_id = ? OR (? IS NOT NULL AND ctfd_user_id = ?))
            """
            params: list[Any] = [member_id, alt_id, user_int, user_int]
            if team_k is not None:
                query += " AND team_k = ?"
                params.append(team_k)
            async with db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return TeamMemberWG(
                    member_id=row["member_id"],
                    team_k=row["team_k"],
                    ctfd_user_id=row["ctfd_user_id"],
                    name=row["name"],
                    client_ip=row["client_ip"],
                    public_key=row["public_key"],
                    private_key=row["private_key"],
                    preshared_key=row["preshared_key"],
                    config_path=row["config_path"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )

    async def list_team_members(self, team_k: int) -> List[TeamMemberWG]:
        """Lists all members assigned to a specific team."""
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT *
                FROM team_members
                WHERE team_k = ?
                ORDER BY created_at ASC
                """,
                (team_k,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    TeamMemberWG(
                        member_id=row["member_id"],
                        team_k=row["team_k"],
                        ctfd_user_id=row["ctfd_user_id"],
                        name=row["name"],
                        client_ip=row["client_ip"],
                        public_key=row["public_key"],
                        private_key=row["private_key"],
                        preshared_key=row["preshared_key"],
                        config_path=row["config_path"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                    for row in rows
                ]

    async def list_used_ips(self, team_k: int) -> List[str]:
        """Returns all client IPs currently allocated to a team."""
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT client_ip
                FROM team_members
                WHERE team_k = ?
                """,
                (team_k,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [row["client_ip"] for row in rows]

    async def delete_team_member(self, member_id: str) -> bool:
        """Deletes a team member record."""
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM team_members WHERE member_id = ?", (member_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def delete_team_member_by_team(self, team_k: int, member_id: str) -> bool:
        """Deletes a team member verified against the specified team."""
        user_int = int(member_id) if str(member_id).isdigit() else None
        alt_id = f"ctfd-{team_k}-{member_id}"
        async with self._connect() as db:
            cursor = await db.execute(
                """
                DELETE FROM team_members
                WHERE team_k = ? AND (member_id = ? OR member_id = ? OR (? IS NOT NULL AND ctfd_user_id = ?))
                """,
                (team_k, member_id, alt_id, user_int, user_int),
            )
            await db.commit()
            return cursor.rowcount > 0

    # --- Distrobuilder Images CRUD Operations ---

    async def claim_image(
        self, alias: str, yaml_sha256: str, yaml_text: str, force: bool = False
    ) -> Tuple[ImageRecord, bool]:
        """Claims or updates an image build slot.

        Returns (record, needs_build):
        - If matching YAML and not force:
          - If 'ready': returns (record, False).
          - If 'building': returns (record, False).
        - If YAML changed, or status was 'failed', or force=True:
          - If status is 'building' and not force: raises StateConflictError("Image '{alias}' is currently building").
          - Updates row to 'building', new sha256, new yaml_text, error=None, updated_at=now and returns (record, True).
        - If new: creates 'building' and returns (record, True).
        """
        now_str = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            async with db.execute("SELECT * FROM images WHERE alias = ?", (alias,)) as cursor:
                existing_alias = await cursor.fetchone()

            if existing_alias:
                rec_status = existing_alias["status"]
                same_sha = existing_alias["yaml_sha256"] == yaml_sha256

                if not force and same_sha:
                    if rec_status == "ready":
                        return ImageRecord(
                            alias=alias,
                            yaml_sha256=existing_alias["yaml_sha256"],
                            yaml_text=existing_alias["yaml_text"],
                            status=existing_alias["status"],
                            fingerprint=existing_alias["fingerprint"],
                            error=existing_alias["error"],
                            created_at=datetime.fromisoformat(existing_alias["created_at"]),
                            updated_at=datetime.fromisoformat(existing_alias["updated_at"]),
                        ), False
                    elif rec_status == "building":
                        return ImageRecord(
                            alias=alias,
                            yaml_sha256=existing_alias["yaml_sha256"],
                            yaml_text=existing_alias["yaml_text"],
                            status=existing_alias["status"],
                            fingerprint=existing_alias["fingerprint"],
                            error=existing_alias["error"],
                            created_at=datetime.fromisoformat(existing_alias["created_at"]),
                            updated_at=datetime.fromisoformat(existing_alias["updated_at"]),
                        ), False

                if rec_status == "building" and not force:
                    raise StateConflictError(
                        f"Image '{alias}' is currently building"
                    )

                await db.execute(
                    """
                    UPDATE images
                    SET yaml_sha256 = ?, yaml_text = ?, status = 'building', error = NULL, updated_at = ?
                    WHERE alias = ?
                    """,
                    (yaml_sha256, yaml_text, now_str, alias),
                )
                await db.commit()
                return ImageRecord(
                    alias=alias,
                    yaml_sha256=yaml_sha256,
                    yaml_text=yaml_text,
                    status="building",
                    fingerprint=existing_alias["fingerprint"],
                    error=None,
                    created_at=datetime.fromisoformat(existing_alias["created_at"]),
                    updated_at=datetime.fromisoformat(now_str),
                ), True

            # Completely new image
            await db.execute(
                """
                INSERT INTO images (alias, yaml_sha256, yaml_text, status, fingerprint, error, created_at, updated_at)
                VALUES (?, ?, ?, 'building', NULL, NULL, ?, ?)
                """,
                (alias, yaml_sha256, yaml_text, now_str, now_str),
            )
            await db.commit()
            return ImageRecord(
                alias=alias,
                yaml_sha256=yaml_sha256,
                yaml_text=yaml_text,
                status="building",
                fingerprint=None,
                error=None,
                created_at=datetime.fromisoformat(now_str),
                updated_at=datetime.fromisoformat(now_str),
            ), True

    async def set_image_status(
        self,
        alias: str,
        new_status: str,
        expected_statuses: List[str],
        fingerprint: Optional[str] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Compare-and-swap (CAS) update of image status."""
        now_str = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in expected_statuses)
        sql = f"""
            UPDATE images
            SET status = ?, fingerprint = COALESCE(?, fingerprint), error = ?, updated_at = ?
            WHERE alias = ? AND status IN ({placeholders})
        """
        params = [new_status, fingerprint, error, now_str, alias] + expected_statuses
        async with self._connect() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount > 0

    async def get_image(self, alias: str) -> Optional[ImageRecord]:
        """Retrieves image record by alias."""
        async with self._connect() as db:
            async with db.execute("SELECT * FROM images WHERE alias = ?", (alias,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return ImageRecord(
                    alias=row["alias"],
                    yaml_sha256=row["yaml_sha256"],
                    yaml_text=row["yaml_text"],
                    status=row["status"],
                    fingerprint=row["fingerprint"],
                    error=row["error"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )

    async def list_images(self) -> List[ImageRecord]:
        """Lists all image records ordered by created_at desc."""
        async with self._connect() as db:
            async with db.execute("SELECT * FROM images ORDER BY created_at DESC") as cursor:
                rows = await cursor.fetchall()
                return [
                    ImageRecord(
                        alias=row["alias"],
                        yaml_sha256=row["yaml_sha256"],
                        yaml_text=row["yaml_text"],
                        status=row["status"],
                        fingerprint=row["fingerprint"],
                        error=row["error"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                        updated_at=datetime.fromisoformat(row["updated_at"]),
                    )
                    for row in rows
                ]

    async def delete_image(self, alias: str) -> bool:
        """Deletes an image from the database.

        Will raise sqlite3.IntegrityError if active challenge instances reference it.
        """
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM images WHERE alias = ?", (alias,))
            await db.commit()
            return cursor.rowcount > 0

    # --- Running Container Operations ---

    async def record_container(
        self,
        instance_name: str,
        team_k: int,
        spec_name: str,
        ip_address: str,
        image_alias: str,
    ) -> RunningContainerRecord:
        """Records or updates a running container for a team."""
        now_str = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO running_containers (instance_name, team_k, spec_name, ip_address, image_alias, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_name) DO UPDATE SET
                    spec_name=excluded.spec_name,
                    ip_address=excluded.ip_address,
                    image_alias=excluded.image_alias
                """,
                (instance_name, team_k, spec_name, ip_address, image_alias, now_str),
            )
            await db.commit()
            return RunningContainerRecord(
                instance_name=instance_name,
                team_k=team_k,
                spec_name=spec_name,
                ip_address=ip_address,
                image_alias=image_alias,
                created_at=datetime.fromisoformat(now_str),
            )

    async def get_container(self, instance_name: str) -> Optional[RunningContainerRecord]:
        """Retrieves a running container by its instance name."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM running_containers WHERE instance_name = ?", (instance_name,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return RunningContainerRecord(
                    instance_name=row["instance_name"],
                    team_k=row["team_k"],
                    spec_name=row["spec_name"],
                    ip_address=row["ip_address"],
                    image_alias=row["image_alias"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )

    async def get_container_by_spec(self, team_k: int, spec_name: str) -> Optional[RunningContainerRecord]:
        """Retrieves a running container for a team by its spec name."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM running_containers WHERE team_k = ? AND spec_name = ?", (team_k, spec_name)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return RunningContainerRecord(
                    instance_name=row["instance_name"],
                    team_k=row["team_k"],
                    spec_name=row["spec_name"],
                    ip_address=row["ip_address"],
                    image_alias=row["image_alias"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )

    async def list_team_containers(self, team_k: int) -> List[RunningContainerRecord]:
        """Lists all running containers belonging to a team."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM running_containers WHERE team_k = ? ORDER BY spec_name ASC", (team_k,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    RunningContainerRecord(
                        instance_name=row["instance_name"],
                        team_k=row["team_k"],
                        spec_name=row["spec_name"],
                        ip_address=row["ip_address"],
                        image_alias=row["image_alias"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                    for row in rows
                ]

    async def list_all_containers(self) -> List[RunningContainerRecord]:
        """Lists all running containers across all teams."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM running_containers ORDER BY team_k ASC, spec_name ASC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    RunningContainerRecord(
                        instance_name=row["instance_name"],
                        team_k=row["team_k"],
                        spec_name=row["spec_name"],
                        ip_address=row["ip_address"],
                        image_alias=row["image_alias"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                    for row in rows
                ]

    async def delete_container(self, instance_name: str) -> bool:
        """Deletes a container record from running_containers."""
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM running_containers WHERE instance_name = ?", (instance_name,))
            await db.commit()
            return cursor.rowcount > 0

    async def delete_all_team_containers(self, team_k: int) -> int:
        """Deletes all containers for a team."""
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM running_containers WHERE team_k = ?", (team_k,))
            await db.commit()
            return cursor.rowcount


    async def reconcile_startup_states(self) -> Dict[str, int]:
        """Reconciles interrupted in-flight states upon server reboot."""
        now_str = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            img_cursor = await db.execute(
                """
                UPDATE images
                SET status = 'failed', error = 'Interrupted by system restart', updated_at = ?
                WHERE status = 'building'
                """,
                (now_str,),
            )
            await db.commit()
            return {
                "challenges_reconciled": 0,
                "images_reconciled": img_cursor.rowcount,
            }
