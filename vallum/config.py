"""Policy, catalog and environment. Enforcement loads policy from the default branch (FR-RULE-4)."""

import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VALLUM_DIR = ROOT / "vallum"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_yaml_text(text: str):
    return yaml.safe_load(text) or {}


def load_local(name: str) -> dict:
    return load_yaml_text((VALLUM_DIR / name).read_text(encoding="utf-8"))


def load_from_default_branch(name: str, gitlab=None) -> dict:
    """On main, the checkout is the default branch. Anywhere else, fetch the file from main
    through the API, so a branch can never change the rules it is judged by."""
    branch = os.environ.get("CI_COMMIT_BRANCH")
    default = os.environ.get("CI_DEFAULT_BRANCH", "main")
    if gitlab is None or branch == default:
        return load_local(name)
    return load_yaml_text(gitlab.file_raw(f"vallum/{name}", ref=default))


class Policy:
    def __init__(self, data: dict):
        self.data = data

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def owners(self) -> list[str]:
        return list(self.data.get("owners", []))

    @property
    def demo(self) -> bool:
        return bool(self.data.get("demo", False))

    def label(self, name: str) -> str:
        return self.data["labels"][name]

    def tier(self, name: str) -> dict:
        return self.data["tiers"][name]

    def watch_seconds(self, tier: str) -> float:
        t = self.tier(tier)
        minutes = t["demo_watch_min"] if self.demo else t["watch_min"]
        return float(minutes) * 60

    def min_samples(self, kind: str) -> int:
        table = self.data["judge"]["demo_min_samples" if self.demo else "min_samples"]
        return int(table.get(kind, table.get("speed", 100)))

    def steps(self, tier: str, mode: str = "green") -> list[int]:
        t = self.tier(tier)
        steps = list(t["steps"])
        extra = t.get("yellow_extra_step")
        if mode == "yellow" and extra and extra not in steps:
            steps = sorted(steps + [extra])
        return steps

    def window_minutes(self) -> float:
        w = self.data["windows"]
        return float(w["demo_minutes"]) if self.demo else float(w["production_days"]) * 24 * 60


def policy(gitlab=None) -> Policy:
    return Policy(load_from_default_branch("policy.yaml", gitlab))


def catalog(gitlab=None) -> dict:
    return load_from_default_branch("catalog.yaml", gitlab)


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Missing required environment variable {name}")
    return value


def project_id() -> str:
    return os.environ.get("CI_PROJECT_ID", "87894346")


def running_stale_code(gl) -> str | None:
    """A main-pipeline job running older code than main has now (it waited in a queue, or a newer push landed).
    Returns main's head if so. Such a job must not act: the job of the newer pipeline will (seen live: a desk job
    queued for 20 minutes re-applied a verdict that newer code had already discarded)."""
    sha = os.environ.get("CI_COMMIT_SHA")
    if not sha:
        return None
    try:
        head = gl.branch_head(os.environ.get("CI_DEFAULT_BRANCH", "main"))
    except Exception:
        return None
    return head if head and head != sha else None


def service_url(pol: Policy) -> str:
    cr = pol["cloud_run"]
    number = os.environ.get("GCP_PROJECT_NUMBER", "256944994567")
    return f"https://{cr['service']}-{number}.{cr['region']}.run.app"
