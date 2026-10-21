"""Start a copy of the app on localhost inside the CI job (FR-JUDGE-4: no cloud credentials)."""

import io
import os
import socket
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx


def wait_for_port(host: str, port: int, timeout: float = 90) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


@contextmanager
def local_app(source_dir: Path, port: int, extra_env: dict | None = None, log_path: Path | None = None):
    """Run uvicorn from source_dir and yield its base URL once /health answers."""
    env = {**os.environ, "APP_LOG_REQUESTS": "0", "K_REVISION": f"local-{port}", **(extra_env or {})}
    env.pop("K_SERVICE", None)
    log = open(log_path, "wb") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        cwd=str(source_dir), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"the app in {source_dir} exited with code {proc.returncode} before it was ready")
            try:
                if httpx.get(base + "/health", timeout=2).status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"the app in {source_dir} did not answer /health within 60 s")
            time.sleep(0.5)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if log_path:
            log.close()


def fetch_main_source(gitlab, dest: Path, ref: str = "main") -> Path:
    """Download the default branch as a tarball (public project, no token) for the fail-first check."""
    resp = gitlab.request("GET", "/projects/:id/repository/archive.tar.gz", params={"sha": ref}, raw=True)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if not target.is_relative_to(root) or not (member.isfile() or member.isdir()):
                continue  # only plain files and folders inside dest
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is not None:
                target.write_bytes(source.read())
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise RuntimeError("unexpected archive layout")
    return tops[0]
