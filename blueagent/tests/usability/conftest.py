"""Pytest fixtures for BlueAgent usability tests."""

import os
import subprocess
import time
import pytest
from pathlib import Path
from .client import BlueAgentTestClient, DEFAULT_BASE_URL, DEFAULT_TOKEN

PORT = 8000
SERVER_URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session")
def client():
    """Provides an authenticated BlueAgentTestClient ensuring server is running."""
    test_client = BlueAgentTestClient(base_url=SERVER_URL, token=DEFAULT_TOKEN)
    proc = None

    # Always terminate any stale server to load latest codebase
    subprocess.run(["sudo", "pkill", "-9", "-f", f"port {PORT}"], check=False)
    subprocess.run(["sudo", "pkill", "-9", "-f", "uvicorn blueagent.main:app"], check=False)
    time.sleep(0.5)

    src_path = str(Path(__file__).resolve().parent.parent.parent / "src")
    log_file = Path("/tmp/blueagent_usability.log")
    log_fd = open(log_file, "w")
    cmd = [
        "sudo",
        "-E",
        "env",
        f"PYTHONPATH={src_path}",
        f"BLUEAGENT_CTFD_API_TOKEN={DEFAULT_TOKEN}",
        "python3",
        "-m",
        "uvicorn",
        "blueagent.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(PORT),
    ]
    proc = subprocess.Popen(cmd, stdout=log_fd, stderr=log_fd)

    # Wait for server to start
    start_time = time.time()
    ready = False
    while time.time() - start_time < 20.0:
        try:
            import urllib.request
            with urllib.request.urlopen(f"{SERVER_URL}/health", timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        if proc:
            subprocess.run(["sudo", "kill", "-9", str(proc.pid)], check=False)
        raise RuntimeError(f"Server failed to start on port {PORT}. Check {log_file}")

    yield test_client

    if proc is not None:
        # Stop the server we started
        subprocess.run(["sudo", "kill", "-9", str(proc.pid)], check=False)
        subprocess.run(["sudo", "pkill", "-9", "-f", f"port {PORT}"], check=False)
