import asyncio
import hashlib
import io
import logging
from pathlib import Path
import re
import sqlite3
import tarfile
from typing import List, Optional, Tuple
import zipfile
import yaml

from ..config import settings
from ..database import DatabaseManager, StateConflictError
from ..engine.base import ContainerEngine
from ..images.distrobuilder import ImageBuilder
from ..models import ImageRecord

logger = logging.getLogger(__name__)

ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class ImageServiceError(Exception):
    """Base exception for image service operations."""


class ImageYamlTooLargeError(ImageServiceError):
    """Raised when submitted image definition YAML or archive exceeds maximum size."""


class ImageYamlInvalidError(ImageServiceError):
    """Raised when submitted YAML is invalid or archive missing definition.yaml."""


class ImageAliasInvalidError(ImageServiceError):
    """Raised when target image alias is invalid."""


class ImageStateConflictError(ImageServiceError):
    """Raised when claiming image conflicts with an existing different definition."""


class ImageInUseConflictError(ImageServiceError):
    """Raised when deleting an image that is currently referenced by containers."""


class ImageNotFoundError(ImageServiceError):
    """Raised when an image alias is not found."""


class ImageService:
    """Orchestrates image registration, compilation, and catalog maintenance."""

    def __init__(
        self,
        db: DatabaseManager,
        builder: ImageBuilder,
        engine: ContainerEngine,
    ):
        self.db = db
        self.builder = builder
        self.engine = engine

    async def submit_image(
        self,
        content_bytes: bytes,
        alias: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[ImageRecord, bool]:
        """Validates and claims a Distrobuilder image specification or context archive.

        Accepts either a raw UTF-8 definition.yaml or a compressed archive (.tar.gz, .zip)
        containing definition.yaml at its root plus auxiliary challenge source files.

        Returns a tuple of (ImageRecord, is_accepted_pending_build).
        """
        if alias is not None and not ALIAS_PATTERN.match(alias):
            raise ImageAliasInvalidError(
                f"Image alias '{alias}' must match regex ^[a-z0-9][a-z0-9-]{{0,31}}$"
            )

        is_archive = False
        yaml_text: Optional[str] = None

        # Check if submitted content is a tarball
        if tarfile.is_tarfile(io.BytesIO(content_bytes)):
            is_archive = True
            if len(content_bytes) > settings.image_archive_max_bytes:
                raise ImageYamlTooLargeError(
                    f"Image build context archive exceeds {settings.image_archive_max_bytes} bytes limit"
                )
            try:
                with tarfile.open(fileobj=io.BytesIO(content_bytes), mode="r:*") as tf:
                    names = tf.getnames()
                    yaml_names = [
                        n for n in names
                        if n in ("definition.yaml", "./definition.yaml")
                        or (n.endswith("/definition.yaml") and n.count("/") == 1)
                    ]
                    target_name = "definition.yaml" if "definition.yaml" in names else (
                        yaml_names[0] if yaml_names else None
                    )
                    if not target_name:
                        raise ImageYamlInvalidError(
                            "Archive must contain a 'definition.yaml' file at its root"
                        )
                    extracted = tf.extractfile(target_name)
                    if not extracted:
                        raise ImageYamlInvalidError("Could not extract 'definition.yaml' from archive")
                    yaml_text = extracted.read().decode("utf-8")
            except ImageYamlInvalidError:
                raise
            except Exception as exc:
                raise ImageYamlInvalidError(f"Failed to inspect archive definition.yaml: {exc}") from exc

        # Check if submitted content is a zip archive
        elif zipfile.is_zipfile(io.BytesIO(content_bytes)):
            is_archive = True
            if len(content_bytes) > settings.image_archive_max_bytes:
                raise ImageYamlTooLargeError(
                    f"Image build context archive exceeds {settings.image_archive_max_bytes} bytes limit"
                )
            try:
                with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
                    names = zf.namelist()
                    yaml_names = [
                        n for n in names
                        if n in ("definition.yaml", "./definition.yaml")
                        or (n.endswith("/definition.yaml") and n.count("/") == 1)
                    ]
                    target_name = "definition.yaml" if "definition.yaml" in names else (
                        yaml_names[0] if yaml_names else None
                    )
                    if not target_name:
                        raise ImageYamlInvalidError(
                            "Archive must contain a 'definition.yaml' file at its root"
                        )
                    yaml_text = zf.read(target_name).decode("utf-8")
            except ImageYamlInvalidError:
                raise
            except Exception as exc:
                raise ImageYamlInvalidError(f"Failed to inspect archive definition.yaml: {exc}") from exc

        # Otherwise treat as raw Distrobuilder YAML definition
        else:
            if len(content_bytes) > settings.image_yaml_max_bytes:
                raise ImageYamlTooLargeError(
                    f"Image definition YAML exceeds {settings.image_yaml_max_bytes} bytes limit"
                )
            try:
                yaml_text = content_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ImageYamlInvalidError(
                    f"Content is neither a valid archive nor UTF-8 YAML: {exc}"
                ) from exc

        try:
            parsed = yaml.safe_load(yaml_text)
            if not isinstance(parsed, dict):
                raise ValueError("YAML root must be a mapping/dictionary")
        except Exception as exc:
            raise ImageYamlInvalidError(f"Invalid YAML definition: {exc}") from exc

        sha256 = hashlib.sha256(content_bytes).hexdigest()
        target_alias = alias or f"ba-{sha256[:12]}"

        # Cache build context archive for ImageBuilder if archive was provided
        build_base = Path(settings.image_build_dir)
        build_base.mkdir(parents=True, exist_ok=True)
        context_file = build_base / f"{target_alias}-context.bin"
        if is_archive:
            context_file.write_bytes(content_bytes)
        else:
            context_file.unlink(missing_ok=True)

        try:
            record, needs_build = await self.db.claim_image(
                alias=target_alias,
                yaml_sha256=sha256,
                yaml_text=yaml_text,
                force=force,
            )
        except StateConflictError as exc:
            raise ImageStateConflictError(str(exc)) from exc

        if needs_build:
            asyncio.create_task(self.builder.build_image(target_alias))
            return record, True

        is_pending = record.status == "building"
        return record, is_pending

    async def get_image_logs(self, alias: str) -> Optional[str]:
        """Retrieves compilation and import logs for an image alias if present."""
        img = await self.db.get_image(alias)
        if not img:
            return None

        build_base = Path(settings.image_build_dir)
        log_path = build_base / f"{alias}.log"
        if log_path.is_file():
            try:
                return log_path.read_text(encoding="utf-8", errors="replace")
            except Exception as err:
                logger.warning("Error reading logs for image '%s': %s", alias, err)

        if img.error:
            return f"Build error:\n{img.error}\n"
        return f"No logs recorded yet for image '{alias}' (status: {img.status})\n"

    async def list_images(self) -> List[ImageRecord]:
        """Lists all registered distrobuilder images and their build states."""
        return await self.db.list_images()

    async def get_image(self, alias: str) -> Optional[ImageRecord]:
        """Retrieves metadata and status for a specific image alias."""
        return await self.db.get_image(alias)

    async def delete_image(self, alias: str) -> bool:
        """Deletes an image from the database and Incus engine.

        Returns True if image existed and was deleted, False if not found.
        """
        img = await self.db.get_image(alias)
        if not img:
            return False

        # 1. Attempt database deletion first to enforce foreign key constraints
        try:
            await self.db.delete_image(alias)
        except sqlite3.IntegrityError as err:
            raise ImageInUseConflictError(
                f"Cannot delete image '{alias}' because active challenge containers reference it"
            ) from err

        # 2. Only if DB deletion succeeded, delete Incus alias and image
        try:
            await self.engine.delete_image_alias(alias)
            if img.fingerprint:
                await self.engine.delete_image(img.fingerprint)
        except Exception as exc:
            logger.warning("Error deleting engine image/alias for '%s': %s", alias, exc)

        # 3. Clean up log file and context archive from disk
        build_base = Path(settings.image_build_dir)
        (build_base / f"{alias}.log").unlink(missing_ok=True)
        (build_base / f"{alias}-context.bin").unlink(missing_ok=True)

        return True
