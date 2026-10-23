"""Start a copy of the app on localhost inside the CI job (FR-JUDGE-4: no cloud credentials)."""

import io
import os
import shlex
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


DEFAULT_START = "{python} -m uvicorn app.main:app --host 127.0.0.1 --port {port} --no-access-log"


def app_commands(profile: dict | None = None) -> tuple[str, str]:
    """How to start this codebase and where it answers when ready, from vallum/project.yaml (B29)."""
    if profile is None:
        from vallum.codemap import load_profile
        from vallum.config import ROOT

        try:
            profile = load_profile(ROOT)
        except SystemExit:
            profile = {}
    cmds = (profile or {}).get("commands") or {}
    return cmds.get("start") or DEFAULT_START, cmds.get("health") or "/health"


def start_argv(template: str, port: int) -> list[str]:
    return [t.format(python=sys.executable, port=port) for t in shlex.split(template)]


@contextmanager
def local_app(source_dir: Path, port: int, extra_env: dict | None = None, log_path: Path | None = None,
              profile: dict | None = None):
    """Start the app from source_dir with the profile's start command; yield its base URL once it is healthy."""
    start, health = app_commands(profile)
    env = {**os.environ, "APP_LOG_REQUESTS": "0", "K_REVISION": f"local-{port}", "PORT": str(port), **(extra_env or {})}
    env.pop("K_SERVICE", None)
    log = open(log_path, "wb") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(start_argv(start, port), cwd=str(source_dir), env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"the app in {source_dir} exited with code {proc.returncode} before it was ready")
            try:
                if httpx.get(base + health, timeout=2).status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"the app in {source_dir} did not answer {health} within 60 s")
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
