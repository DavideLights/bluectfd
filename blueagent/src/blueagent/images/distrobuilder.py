"""Distrobuilder image build pipeline for BlueAgent v2.

Builds reproducible Incus container images from Distrobuilder YAML definitions,
imports the resulting unified image tarball into Incus, assigns the requested
alias, and updates relational image metadata state with write-ahead CAS semantics.
"""

import asyncio
import logging
from pathlib import Path
import shutil
import tempfile
from typing import Optional
import yaml

from ..config import settings
from ..database import DatabaseManager
from ..engine.base import ContainerEngine
from ..locks import LockRegistry

logger = logging.getLogger(__name__)


def _ensure_network_dirs_in_definition(raw_yaml: str) -> str:
    """Injects creation of network config directories into post-files actions if missing."""
    try:
        data = yaml.safe_load(raw_yaml)
        if not isinstance(data, dict):
            return raw_yaml
        actions = data.setdefault("actions", [])
        if not isinstance(actions, list):
            return raw_yaml
        has_net_dirs = any(
            isinstance(act, dict) and "mkdir -p /etc/systemd/network /etc/network" in str(act.get("action", ""))
            for act in actions
        )
        if not has_net_dirs:
            actions.append({
                "trigger": "post-files",
                "action": "#!/bin/sh\nmkdir -p /etc/systemd/network /etc/network\n",
            })
            return yaml.dump(data, sort_keys=False)
        return raw_yaml
    except Exception as exc:
        logger.warning("Failed to inject network directories into definition YAML: %s", exc)
        return raw_yaml


class ImageBuilder:
    """Manages compilation, import, and lifecycle of Distrobuilder container images."""

    def __init__(
        self,
        db: DatabaseManager,
        engine: ContainerEngine,
        locks: LockRegistry,
    ):
        self.db = db
        self.engine = engine
        self.locks = locks

    async def build_image(self, alias: str) -> None:
        """Executes distrobuilder pipeline in background under global per-alias lock."""
        async with self.locks.lock_global(f"image:{alias}"):
            img = await self.db.get_image(alias)
            if not img or img.status != "building":
                logger.info("Image '%s' not in 'building' state, skipping build.", alias)
                return

            old_fingerprint: Optional[str] = img.fingerprint
            build_base = Path(settings.image_build_dir)
            build_base.mkdir(parents=True, exist_ok=True)
            log_path = build_base / f"{alias}.log"

            def _log(line: str) -> None:
                try:
                    with open(log_path, "a", encoding="utf-8") as lf:
                        lf.write(line + "\n")
                except Exception as log_err:
                    logger.warning("Error writing to %s: %s", log_path, log_err)

            # Initialize log file
            try:
                log_path.write_text(f"=== Starting Distrobuilder build for image '{alias}' ===\n", encoding="utf-8")
            except Exception as init_err:
                logger.warning("Could not initialize log file %s: %s", log_path, init_err)

            tmp_dir = Path(tempfile.mkdtemp(dir=str(build_base), prefix=f"build-{alias}-"))
            fingerprint: Optional[str] = None

            try:
                # 1. Unpack context archive if submitted
                context_file = build_base / f"{alias}-context.bin"
                if context_file.is_file():
                    _log(f"Extracting build context archive into {tmp_dir}...")
                    try:
                        shutil.unpack_archive(str(context_file), str(tmp_dir))
                        _log("Build context unpacked successfully.")
                    finally:
                        context_file.unlink(missing_ok=True)

                # 2. Write definition.yaml with pre-baked network dirs
                final_yaml = _ensure_network_dirs_in_definition(img.yaml_text)
                yaml_file = tmp_dir / "definition.yaml"
                yaml_file.write_text(final_yaml, encoding="utf-8")

                cmd = [
                    settings.distrobuilder_bin,
                    "build-incus",
                    str(yaml_file.name),
                    "--type=unified",
                ]
                if settings.use_sudo_distrobuilder:
                    cmd = ["sudo", "-n"] + cmd

                _log(f"Executing: {' '.join(cmd)}")
                logger.info("Starting distrobuilder for '%s' in %s", alias, tmp_dir)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(tmp_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                stdout_tail = []

                if getattr(proc, "stdout", None) is not None:
                    async def _stream_output():
                        while True:
                            line = await proc.stdout.readline()
                            if not line:
                                break
                            decoded = line.decode("utf-8", errors="replace")
                            stdout_tail.append(decoded)
                            if len(stdout_tail) > 50:
                                stdout_tail.pop(0)
                            try:
                                with open(log_path, "a", encoding="utf-8") as lf:
                                    lf.write(decoded)
                            except Exception:
                                pass

                    try:
                        await asyncio.wait_for(
                            asyncio.gather(proc.wait(), _stream_output()),
                            timeout=float(settings.image_build_timeout),
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        timeout_msg = (
                            f"Distrobuilder image build for '{alias}' exceeded timeout of {settings.image_build_timeout}s"
                        )
                        _log(f"ERROR: {timeout_msg}")
                        raise TimeoutError(timeout_msg)
                else:
                    try:
                        out_bytes, err_bytes = await asyncio.wait_for(
                            proc.communicate(),
                            timeout=float(settings.image_build_timeout),
                        )
                        out_text = (out_bytes or b"").decode("utf-8", errors="replace")
                        err_text = (err_bytes or b"").decode("utf-8", errors="replace")
                        if out_text:
                            stdout_tail.append(out_text)
                            _log(out_text)
                        if err_text:
                            stdout_tail.append(err_text)
                            _log(err_text)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        timeout_msg = (
                            f"Distrobuilder image build for '{alias}' exceeded timeout of {settings.image_build_timeout}s"
                        )
                        _log(f"ERROR: {timeout_msg}")
                        raise TimeoutError(timeout_msg)

                if proc.returncode != 0:
                    err_summary = "".join(stdout_tail[-20:])
                    err_msg = f"Distrobuilder exited with code {proc.returncode}:\n{err_summary}"
                    _log(f"ERROR: {err_msg}")
                    raise RuntimeError(err_msg)

                # Locate the unified tarball (*.tar.xz or *.tar.gz)
                tarballs = list(tmp_dir.glob("*.tar.xz")) + list(tmp_dir.glob("*.tar.gz"))
                if not tarballs:
                    raise FileNotFoundError(f"Distrobuilder did not produce unified image tarball in {tmp_dir}")
                tarball_path = tarballs[0]

                _log(f"Importing image tarball {tarball_path.name} into Incus...")
                logger.info("Importing image tarball %s into Incus for alias '%s'", tarball_path, alias)
                fingerprint = await self.engine.import_image(str(tarball_path))
                _log(f"Imported fingerprint: {fingerprint}")

                _log(f"Creating Incus alias '{alias}' -> {fingerprint}...")
                logger.info("Creating Incus alias '%s' for fingerprint %s", alias, fingerprint)
                await self.engine.create_image_alias(alias, fingerprint)

                # If an older fingerprint was superseded, remove it from Incus to free disk space
                if old_fingerprint and old_fingerprint != fingerprint:
                    _log(f"Pruning superseded Incus image fingerprint {old_fingerprint}...")
                    try:
                        await self.engine.delete_image(old_fingerprint)
                        _log(f"Superseded image {old_fingerprint} deleted from Incus.")
                    except Exception as clean_old_err:
                        logger.warning("Could not delete superseded Incus image %s: %s", old_fingerprint, clean_old_err)
                        _log(f"Warning: could not delete superseded image {old_fingerprint}: {clean_old_err}")

                # Update database status to ready
                updated = await self.db.set_image_status(
                    alias=alias,
                    new_status="ready",
                    expected_statuses=["building"],
                    fingerprint=fingerprint,
                )
                if not updated:
                    logger.warning("CAS to ready failed for image '%s'", alias)
                else:
                    logger.info("Image '%s' built and marked ready successfully.", alias)
                    _log(f"=== Image '{alias}' successfully built and marked ready ===")

            except Exception as exc:
                logger.exception("Failed to build image '%s': %s", alias, exc)
                _log(f"BUILD FAILED: {exc}")
                if fingerprint:
                    try:
                        await self.engine.delete_image(fingerprint)
                    except Exception as clean_err:
                        logger.warning("Failed to clean up partial Incus image %s: %s", fingerprint, clean_err)

                await self.db.set_image_status(
                    alias=alias,
                    new_status="failed",
                    expected_statuses=["building"],
                    error=str(exc)[:500],
                )
            finally:
                # Clean up build directory
                try:
                    if settings.use_sudo_distrobuilder:
                        clean_proc = await asyncio.create_subprocess_exec(
                            "sudo", "-n", "rm", "-rf", str(tmp_dir),
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await clean_proc.wait()
                    else:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception as clean_err:
                    logger.warning("Failed to remove temporary build dir %s: %s", tmp_dir, clean_err)
