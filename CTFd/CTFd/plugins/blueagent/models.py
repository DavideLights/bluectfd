"""SQLAlchemy database models for BlueAgent CTFd plugin.

Provides:
- Polymorphic challenge model extension (BlueAgentPatchChallenge)
- Deterministic team_k allocation mapping (1..254) with race-condition resistance
- Deployment tracking per team & challenge
- Detailed verification audit log
"""

from datetime import datetime
import json
import logging
from typing import List, Optional, Set, Tuple

from sqlalchemy.exc import IntegrityError

from CTFd.models import Challenges, db

logger = logging.getLogger(__name__)


class CapacityError(Exception):
    """Raised when team address space 1..254 is completely exhausted."""


class BlueAgentPatchChallenge(Challenges):
    """Custom challenge model for BlueAgent automated patch verification.

    Polymorphically extends the core CTFd Challenges model.
    """

    __tablename__ = "blueagent_challenges"
    __mapper_args__ = {"polymorphic_identity": "blueagent_patch"}

    # Foreign key with CASCADE delete prevents orphan records when challenge deleted
    id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )

    spec_raw = db.Column(db.Text, default="")
    spec_json = db.Column(db.Text, default="{}")
    spec_format = db.Column(db.String(16), default="yaml")
    distro_alias = db.Column(db.String(64), nullable=True)
    auto_deploy = db.Column(db.Boolean, default=True)
    show_verify_details = db.Column(db.Boolean, default=False)
    verify_cooldown = db.Column(db.Integer, default=30)
    allow_flag_fallback = db.Column(db.Boolean, default=False)

    # JSON strings storing container names and ip suffixes for rapid global uniqueness validation
    container_names = db.Column(db.Text, default="[]")
    ip_suffixes = db.Column(db.Text, default="[]")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.type = "blueagent_patch"

    @property
    def parsed_spec(self) -> dict:
        """Parses stored JSON spec string into a Python dict."""
        try:
            return json.loads(self.spec_json or "{}")
        except Exception:
            return {}

    @property
    def parsed_container_names(self) -> List[str]:
        try:
            return json.loads(self.container_names or "[]")
        except Exception:
            return []

    @property
    def parsed_ip_suffixes(self) -> List[int]:
        try:
            return json.loads(self.ip_suffixes or "[]")
        except Exception:
            return []


class BlueAgentTeamMapping(db.Model):
    """Maps CTFd team (or user in Users mode) deterministically to team_k integer (1..254)."""

    __tablename__ = "blueagent_team_mappings"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    account_type = db.Column(db.String(16), nullable=False, default="team")  # "team" or "user"
    team_k = db.Column(db.Integer, unique=True, nullable=False, index=True)
    provisioned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def get_or_create(cls, account_id: int, account_type: str = "team") -> "BlueAgentTeamMapping":
        """Atomically retrieves or creates a team_k mapping in range 1..254.

        Handles race conditions gracefully by using savepoints and retries.
        """
        existing = cls.query.filter_by(account_id=account_id, account_type=account_type).first()
        if existing:
            return existing

        max_retries = 5
        for attempt in range(max_retries):
            # Compute set of currently allocated k values
            allocated_ks: Set[int] = {
                row.team_k for row in db.session.query(cls.team_k).all()
            }

            # Find lowest free k in range 1..254 (0 is reserved for host/infra)
            free_k: Optional[int] = None
            for candidate in range(1, 255):
                if candidate not in allocated_ks:
                    free_k = candidate
                    break

            if free_k is None:
                raise CapacityError(
                    "All 254 team subnet slots (10.1.0.0/16 to 10.254.0.0/16) are currently in use."
                )

            mapping = cls(
                account_id=account_id,
                account_type=account_type,
                team_k=free_k,
                provisioned=False,
            )

            try:
                # Use nested transaction savepoint to isolate race collisions
                with db.session.begin_nested():
                    db.session.add(mapping)
                db.session.commit()
                return mapping
            except IntegrityError:
                db.session.rollback()
                # Re-check if another worker already created the mapping for this account
                existing = cls.query.filter_by(account_id=account_id, account_type=account_type).first()
                if existing:
                    return existing
                # Otherwise another worker grabbed candidate free_k; loop and retry

        raise CapacityError(f"Failed to allocate team_k after {max_retries} concurrent attempts.")


class BlueAgentDeployment(db.Model):
    """Tracks challenge container deployment status per team_k."""

    __tablename__ = "blueagent_deployments"
    __table_args__ = (
        db.UniqueConstraint("team_k", "challenge_id", name="uq_team_challenge_deployment"),
    )

    id = db.Column(db.Integer, primary_key=True)
    team_k = db.Column(db.Integer, nullable=False, index=True)
    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = db.Column(
        db.String(32),
        default="idle",
    )  # "idle", "deploying", "running", "failed", "torn_down"
    last_error = db.Column(db.Text, nullable=True)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    @classmethod
    def get_or_create(cls, team_k: int, challenge_id: int) -> "BlueAgentDeployment":
        record = cls.query.filter_by(team_k=team_k, challenge_id=challenge_id).first()
        if not record:
            record = cls(team_k=team_k, challenge_id=challenge_id, status="idle")
            db.session.add(record)
            db.session.commit()
        return record


class BlueAgentVerifyLog(db.Model):
    """Audit log of verification runs executed through BlueAgent's OVN sandbox."""

    __tablename__ = "blueagent_verify_logs"

    id = db.Column(db.Integer, primary_key=True)
    team_k = db.Column(db.Integer, nullable=False, index=True)
    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    passed = db.Column(db.Boolean, nullable=False)
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.Text, default="")
    details_json = db.Column(db.Text, default="{}")
    duration_ms = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    @property
    def details(self) -> dict:
        try:
            return json.loads(self.details_json or "{}")
        except Exception:
            return {}
