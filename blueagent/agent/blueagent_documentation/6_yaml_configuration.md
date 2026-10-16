# IaC Specification: Challenge Configuration & Data Models

## 1. Pydantic v2 Data Models

BlueAgent validates all challenge declarations, container configurations, and verification payloads using Pydantic v2:

```python
from datetime import datetime, timezone
from typing import List, Dict, Optional, Literal, Any, Union
from pydantic import BaseModel, Field, ConfigDict

class CommandSpec(BaseModel):
    """Specification of an individual verification or readiness command."""
    target: Literal["container", "verifier"] = "container"
    container_name: Optional[str] = None       # Required if target == 'container'
    cmd: List[str]
    env: Dict[str, str] = Field(default_factory=dict)
    timeout: int = Field(15, ge=1, le=300)
    retry_timeout: float = Field(default=15.0, ge=1.0, le=120.0)
    interval: float = Field(default=0.5, ge=0.05, le=10.0)

class ContainerSpec(BaseModel):
    """Specification of an application system container."""
    name: str = Field(..., max_length=56)
    image: str
    ip_suffix: int = Field(..., ge=2, le=254)  # .1 = Gateway, .255 = Broadcast
    limits_cpu: str = "1"
    limits_memory: str = "256MB"

class VerifierSpec(BaseModel):
    """Optional ephemeral test runner container for black-box network checks.
    
    Note: Verifier images must be pre-built and registered in the BlueAgent catalog
    (status == 'ready' via PUT /images/{alias}) prior to challenge deployment.
    """
    image: str = "alpine-verifier"
    ip_suffix: int = 250
    limits_cpu: str = "1"
    limits_memory: str = "256MB"

class ChallengeConfig(BaseModel):
    """Full configuration for challenge deployment and verification.
    
    Note: team_k is extracted from URL path parameters (/teams/{team_k}/...).
    """
    challenge_id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]{0,24}$",
        description="Globally unique identifier for the challenge",
    )
    verify_internet_access: Optional[bool] = Field(
        default=True,
        description="Verification sandboxes allow egress to internet by default via NAT",
    )
    verifier: Optional[VerifierSpec] = Field(
        default=None,
        description="Optional dedicated verifier test runner container",
    )
    containers: List[ContainerSpec] = Field(
        ...,
        min_length=1,
        description="List of container instances forming the challenge topology",
    )
    readiness_commands: List[CommandSpec] = Field(
        default_factory=list,
        description="Optional health check commands executed with retry loops",
    )
    verify_commands: List[CommandSpec] = Field(
        ...,
        min_length=1,
        description="Deterministic verification tests to evaluate functionality or exploits",
    )

class VerifyResultResp(BaseModel):
    """API response returned upon verification completion."""
    success: bool                              # True if test execution pipeline completed
    passed: bool                               # True if all verification checks succeeded
    message: str
    details: Optional[Dict[str, Any]] = None  # Includes step_timings profiling

class TeamPutReq(BaseModel):
    """Payload to create or update a team via PUT /teams/{team_k}."""
    name: str = Field(..., min_length=1, max_length=128, description="Team descriptive name")

class TeamMemberPutReq(BaseModel):
    """Payload to register or update a team member via PUT /teams/{team_k}/members/{user_id}."""
    name: str = Field(..., min_length=1, max_length=128, description="Name or identifier of team member")
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
```

---

## 2. Declarative Challenge Descriptor (`challenge.yaml`)

```yaml
challenge_id: "patch-web-sqli"
title: "SQL Injection and Web Server Patch Management"
description: "SQLi mitigation challenge on Nginx + Python Flask + MySQL stack"
verify_internet_access: true    # Enable/disable NAT uplink inside ephemeral OVN sandbox

# Optional: Ephemeral test runner container for black-box network checks
# (pre-built in BlueAgent catalog with: PUT /images/verifier-python)
verifier:
  image: "verifier-python"
  ip_suffix: 250
  limits_cpu: "1"
  limits_memory: "256MB"

containers:
  - name: "database"
    image: "debian-12-mysql-master"
    ip_suffix: 3                # Maps to 10.k.1.3 (gateway 10.k.1.1)
    limits_cpu: "1"
    limits_memory: "512MB"

  - name: "server"
    image: "ubuntu-2204-flask-master"
    ip_suffix: 2                # Maps to 10.k.1.2
    limits_cpu: "1"
    limits_memory: "256MB"

readiness_commands:             # Health checks executed inside dark OVN sandbox clone before verify_commands
  - target: "container"
    container_name: "database"
    cmd: ["mysqladmin", "ping", "-h", "127.0.0.1"]
    timeout: 15
  - target: "container"
    container_name: "server"
    cmd: ["curl", "-s", "http://127.0.0.1:8080/health"]
    timeout: 10

verify_commands:                # Executed inside ephemeral OVN sandbox
  # 1. In-Container integrity check
  - target: "container"
    container_name: "server"
    cmd: ["systemctl", "is-active", "--quiet", "flask-app"]
    timeout: 5

  # 2. Black-box network exploit check from verifier runner
  - target: "verifier"
    cmd: ["python3", "/opt/checker/test_exploit_sqli.py", "--target", "10.k.1.2"]
    env:
      FLAG_KEY: "test_flag_secret"
    timeout: 15
```
