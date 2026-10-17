"""Subprocess command runner interface and execution adapter for host networking.

Provides an isolated, non-shell async command executor adhering to the Command/Adapter
paradigm, facilitating deterministic unit testing and preventing shell injection vulnerabilities.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence


logger = logging.getLogger(__name__)


class HostCommandError(RuntimeError):
    """Raised when an OS command finishes with an unexpected non-zero exit code."""

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ):
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        cmd_str = " ".join(argv)
        super().__init__(
            f"Host command '{cmd_str}' failed with exit code {returncode}.\n"
            f"Stderr: {stderr.strip()}\nStdout: {stdout.strip()}"
        )


@dataclass(frozen=True)
class CompletedCommand:
    """Represents the terminal execution result of a host command."""

    argv: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Protocol defining the interface for executing host-level commands."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: Optional[bytes] = None,
        check: bool = True,
        timeout: float = 15.0,
    ) -> CompletedCommand:
        """Executes a command asynchronously without spawning a shell."""
        ...


class SubprocessRunner(CommandRunner):
    """Production implementation of CommandRunner utilizing asyncio.create_subprocess_exec."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: Optional[bytes] = None,
        check: bool = True,
        timeout: float = 15.0,
    ) -> CompletedCommand:
        """Executes argv directly via asyncio subprocess."""
        if not argv:
            raise ValueError("Command argv sequence cannot be empty")

        cmd_str = " ".join(argv)
        logger.debug("Executing host command: %s", cmd_str)

        env = dict(os.environ)
        current_path = env.get("PATH", "")
        extra_paths = [p for p in ("/usr/sbin", "/sbin", "/usr/local/sbin") if p not in current_path.split(":")]
        if extra_paths:
            env["PATH"] = f"{':'.join(extra_paths)}:{current_path}"

        executable = shutil.which(argv[0], path=env["PATH"]) if argv else None
        target_bin = executable if executable is not None else argv[0]

        proc = await asyncio.create_subprocess_exec(
            target_bin,
            *argv[1:],
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            out_bytes, err_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin),
                timeout=timeout,
            )
        except asyncio.TimeoutError as err:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            raise TimeoutError(f"Command '{cmd_str}' timed out after {timeout:.1f}s") from err
        stdout = out_bytes.decode("utf-8", errors="replace")
        stderr = err_bytes.decode("utf-8", errors="replace")
        returncode = proc.returncode if proc.returncode is not None else -1


        result = CompletedCommand(
            argv=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

        if check and returncode != 0:
            logger.error(
                "Host command failed (rc=%d): %s\nStderr: %s",
                returncode,
                cmd_str,
                stderr.strip(),
            )
            raise HostCommandError(argv, returncode, stdout, stderr)

        return result
