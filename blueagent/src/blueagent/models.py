"""Pydantic v2 schemas and data validation models for BlueAgent v2.

Encapsulates strict type contracts for:
1. Container runtime specifications and command execution results.
2. IaC challenge definitions and dual-mode verification specifications.
3. Multi-member WireGuard VPN profiles and team management.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class ExecResult(BaseModel):
    """Encapsulates the execution result of an in-container or verifier command."""

    exit_code: int = Field(..., description="Process exit code (0 indicates success)")
    stdout: str = Field(default="", description="Captured standard output stream")
    stderr: str = Field(default="", description="Captured standard error stream")


class CommandSpec(BaseModel):
    """Specification of an automated audit check or exploit execution."""

    target: Literal["container", "verifier"] = Field(
        default="container",
        description="Target execution environment: 'container' for in-situ exec, 'verifier' for network probe",
    )
    container_name: Optional[str] = Field(
        default=None,
        description="Target container name (required when target is 'container')",
    )
    cmd: List[str] = Field(
        ...,
        min_length=1,
        description="Command vector to execute inside the target environment",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Key-value environment variables passed to the process",
    )
    timeout: int = Field(
        default=15,
        ge=1,
        le=300,
        description="Maximum execution duration in seconds before SIGTERM/SIGKILL",
    )
    interval: int = Field(
        default=2,
        ge=1,
        description="Retry interval in seconds (used for readiness commands)",
    )
    retry_timeout: int = Field(
        default=30,
        ge=1,
        description="Total maximum time to retry in seconds (used for readiness commands)",
    )


class ContainerSpec(BaseModel):
    """Hardware and network specification for an application container."""

    name: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]{0,20}$",
        description="Unique hostname and identifier for the container instance (1-21 chars, lowercase alphanumeric and hyphens)",
    )
    image: str = Field(..., description="Distrobuilder image alias (must be built and ready in images repository)")
    ip_suffix: int = Field(
        ...,
        ge=2,
        le=254,
        description="Host byte of static IP on 10.k.1.x/24 (.1 reserved for gateway, .255 for broadcast)",
    )
    limits_cpu: str = Field(default="1", description="Cgroups v2 CPU core limit quota")
    limits_memory: str = Field(default="256MB", description="Cgroups v2 memory limit ceiling")


class VerifierSpec(BaseModel):
    """Specification for an ephemeral runner container attached to the dark OVN sandbox."""

    image: str = Field(
        default="curlimages/curl:latest",
        description="OCI/LXC image alias for black-box network checks",
    )
    ip_suffix: int = Field(
        default=250,
        ge=2,
        le=254,
        description="Host byte of verifier IP within the OVN broadcast domain",
    )
    limits_cpu: str = Field(default="1", description="CPU quota limit for the verifier runner")
    limits_memory: str = Field(default="256MB", description="Memory ceiling for the verifier runner")


class ChallengeConfig(BaseModel):
    """Declarative Infrastructure-as-Code (IaC) configuration for challenge deployment and verification."""

    challenge_id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]{0,24}$",
        description="Globally unique identifier for the challenge (1-25 chars, lowercase alphanumeric and hyphens)",
    )
    verify_internet_access: Optional[bool] = Field(
        default=True,
        description="Verification sandboxes allow egress to internet by default via NAT",
    )
    verifier: Optional[VerifierSpec] = Field(
        default=None,
        description="Optional ephemeral runner container. If omitted, checks execute in-container",
    )
    containers: List[ContainerSpec] = Field(
        default_factory=list,
        description="Collection of system containers comprising the challenge topology",
    )
    readiness_commands: List[CommandSpec] = Field(
        default_factory=list,
        description="Sequential health probes executed post-startup to confirm challenge readiness",
    )
    verify_commands: List[CommandSpec] = Field(
        default_factory=list,
        description="Sequential verification probes and exploit checks",
    )

    def model_post_init(self, __context: Any) -> None:
        # Validate unique container names
        names = [c.name for c in self.containers]
        if len(names) != len(set(names)):
            raise ValueError("Container names must be unique within challenge")

        # Validate unique ip_suffixes
        suffixes = [c.ip_suffix for c in self.containers]
        if len(suffixes) != len(set(suffixes)):
            raise ValueError("Container ip_suffixes must be unique within challenge")

        # Validate verifier ip_suffix doesn't clash with containers
        if self.verifier and self.verifier.ip_suffix in suffixes:
            raise ValueError(f"Verifier ip_suffix {self.verifier.ip_suffix} clashes with container ip_suffix")

        # Validate generated instance name lengths: c-{team_k}-{spec.name} <= 63 chars (team_k max 3 chars)
        for c in self.containers:
            if len(c.name) > 56:
                raise ValueError(f"Container name '{c.name}' exceeds limit (max 56 chars to allow c-team_k- prefix)")


class VerifyResultResp(BaseModel):
    """Structured response detailing the outcome of a verification run."""

    success: bool = Field(
        ...,
        description="True if verification framework executed without infrastructure error",
    )
    passed: bool = Field(
        ...,
        description="True if all verification checks and exploits succeeded with exit_code == 0",
    )
    message: str = Field(..., description="Human-readable summary of the verification result")
    details: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Granular diagnostic telemetry including per-command stdout/stderr",
    )


class TeamPutReq(BaseModel):
    """Payload to create or update a team via PUT /teams/{team_k}."""

    name: str = Field(..., min_length=1, max_length=128, description="Team descriptive name")


class TeamCreateReq(BaseModel):
    """Payload for legacy team creation (if team_k passed in body)."""

    name: str = Field(..., min_length=1, max_length=128, description="Team descriptive name")
    team_k: Optional[int] = Field(default=None, ge=0, le=254, description="Optional team index integer (0-254)")


class TeamMemberPutReq(BaseModel):
    """Payload to register or update a team member via PUT /teams/{team_k}/members/{user_id}."""

    name: str = Field(..., min_length=1, max_length=128, description="Name or identifier of team member")
    public_key: Optional[str] = Field(
        default=None,
        description="Optional client public key; if omitted, a keypair will be generated",
    )


class TeamMemberCreateReq(BaseModel):
    """Legacy payload to generate WireGuard configuration."""

    name: str = Field(..., min_length=1, max_length=128, description="Name or identifier of team member")
    ctfd_user_id: Optional[int] = Field(
        default=None,
        ge=0,
        description="Upstream CTFd user ID.",
    )
    public_key: Optional[str] = Field(
        default=None,
        description="Optional client public key; if omitted, a keypair will be generated",
    )


class PeerTelemetry(BaseModel):
    """WireGuard peer status and bandwidth telemetry for a team member."""

    online: bool = False
    endpoint: Optional[str] = None
    latest_handshake: int = 0
    transfer_rx: int = 0
    transfer_tx: int = 0
    persistent_keepalive: str = "off"


class TeamMemberResp(BaseModel):
    """Clean representation of a team member returned by GET /teams/{team_k}/members."""

    user_id: Union[int, str]
    member_id: str
    team_k: int
    name: str
    client_ip: str
    public_key: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    telemetry: Optional[PeerTelemetry] = None
    step_timings: Optional[Dict[str, float]] = None

    model_config = ConfigDict(from_attributes=True)


class TeamMemberWG(BaseModel):
    """Persisted WireGuard client configuration record for a team member."""

    member_id: str
    team_k: int
    name: str
    client_ip: str
    public_key: str
    ctfd_user_id: Optional[int] = None
    private_key: Optional[str] = None
    preshared_key: Optional[str] = None
    config_path: Optional[str] = None
    config_text: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    telemetry: Optional[PeerTelemetry] = None
    step_timings: Optional[Dict[str, float]] = None

    model_config = ConfigDict(from_attributes=True)


class TeamRecord(BaseModel):
    """Persisted team record."""

    team_k: int
    name: str
    bridge_name: str
    wg_interface: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    step_timings: Optional[Dict[str, float]] = None

    model_config = ConfigDict(from_attributes=True)


class PeerStats(BaseModel):
    """WireGuard peer status and bandwidth telemetry for a team member."""

    member_id: str
    ctfd_user_id: Optional[int] = None
    name: str
    client_ip: str
    public_key: str
    endpoint: Optional[str] = None
    allowed_ips: str
    latest_handshake: int = 0
    transfer_rx: int = 0
    transfer_tx: int = 0
    persistent_keepalive: str = "off"
    online: bool = False


class ImageRecord(BaseModel):
    """Persisted distrobuilder image build record."""

    alias: str
    yaml_sha256: str
    yaml_text: str
    status: Literal["building", "ready", "failed"]
    fingerprint: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(from_attributes=True)


class RunningContainerRecord(BaseModel):
    """Persisted running container instance."""

    instance_name: str
    team_k: int
    spec_name: str
    ip_address: str
    image_alias: str
    challenge_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(from_attributes=True)


ChallengeInstance = RunningContainerRecord


class DeployResponse(BaseModel):
    """Result of deploying containers for a team."""

    team_k: int
    challenge_id: Optional[str] = None
    status: Literal["deploying", "running", "failed", "tearing_down"] = "running"
    config_json: str = "{}"
    deployed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error: Optional[str] = None
    containers: List[RunningContainerRecord] = Field(default_factory=list)
    instances: List[RunningContainerRecord] = Field(default_factory=list)
    step_timings: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


ActiveChallengeRecord = DeployResponse
