"""Abstract Base Class defining the lifecycle contract for container runtimes.

Establishes a uniform interface decoupling orchestration and verification logic
from concrete container backends (Incus LXC system containers, Docker OCI, etc.).
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from ..models import ContainerSpec, ExecResult


class ContainerEngine(ABC):
    """Abstract interface defining standard operations across container runtimes."""

    @abstractmethod
    async def create_instance(
        self,
        spec: ContainerSpec,
        devices: Optional[Dict[str, Any]] = None,
        extra_config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Provisions a new container instance from specification."""
        pass

    @abstractmethod
    async def push_file(
        self,
        name: str,
        path: str,
        content: str | bytes,
        mode: str = "0644",
        uid: int = 0,
        gid: int = 0,
    ) -> None:
        """Uploads a file directly into a container filesystem."""
        pass

    @abstractmethod
    async def image_alias_exists(self, alias: str) -> bool:
        """Checks if an image alias exists in the container engine."""
        pass

    @abstractmethod
    async def import_image(self, tarball_path: str) -> str:
        """Imports a unified container image tarball and returns its fingerprint."""
        pass

    @abstractmethod
    async def create_image_alias(self, alias: str, fingerprint: str) -> None:
        """Creates an alias pointing to an image fingerprint."""
        pass

    @abstractmethod
    async def delete_image_alias(self, alias: str) -> None:
        """Deletes an image alias."""
        pass

    @abstractmethod
    async def delete_image(self, fingerprint: str) -> None:
        """Deletes an image by fingerprint."""
        pass

    @abstractmethod
    async def start_instance(self, name: str) -> None:
        """Powers on an existing container instance."""
        pass

    @abstractmethod
    async def stop_instance(self, name: str, force: bool = False) -> None:
        """Stops an active container instance."""
        pass

    @abstractmethod
    async def delete_instance(self, name: str) -> None:
        """Deletes an instance and releases its storage volume."""
        pass

    @abstractmethod
    async def clone_instance(self, source: str, target: str) -> str:
        """Creates a fast Copy-on-Write (CoW) clone of the source instance."""
        pass

    @abstractmethod
    async def connect_network(
        self,
        instance_name: str,
        network_name: str,
        ip_address: Optional[str] = None,
    ) -> None:
        """Attaches a container instance to a designated network (OVN switch or bridge)."""
        pass

    @abstractmethod
    async def disconnect_network(self, instance_name: str, network_name: str) -> None:
        """Detaches a container instance from a network."""
        pass

    @abstractmethod
    async def exec_command(
        self,
        name: str,
        cmd: List[str],
        env: Optional[Dict[str, str]] = None,
        timeout: int = 15,
        capture_output: bool = True,
    ) -> ExecResult:
        """Executes a command vector inside the container and returns exit code and streams."""
        pass

    @abstractmethod
    async def get_instance_status(self, name: str) -> str:
        """Queries and returns the current instance lifecycle state (e.g., 'RUNNING', 'STOPPED')."""
        pass

    @abstractmethod
    async def create_network(self, name: str, config: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Provisions an isolated software-defined logical network."""
        pass

    @abstractmethod
    async def delete_network(self, name: str) -> None:
        """Deletes an isolated logical network and reclaims allocated IP ranges."""
        pass

    @abstractmethod
    async def list_instances(self, prefix: Optional[str] = None) -> List[str]:
        """Lists instance names, optionally filtered by a name prefix (e.g. 'verify-')."""
        pass

    @abstractmethod
    async def instance_exists(self, name: str) -> bool:
        """Determines whether a container instance exists."""
        pass

    async def list_networks(self, prefix: Optional[str] = None) -> List[str]:
        """Lists network names, optionally filtered by a prefix (e.g. 'verify-')."""
        return []

    async def get_instance_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Retrieves full instance details and configuration metadata."""
        return None

    @abstractmethod
    async def aclose(self) -> None:
        """Closes all underlying network sessions, sockets, and client transports."""
        pass
