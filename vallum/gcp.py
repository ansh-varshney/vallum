"""Keyless Google Cloud login for main-pipeline jobs (Workload Identity Federation, FR-GCP-1).

GitLab gives the job a short-lived OIDC token (id_tokens: GCP_ID_TOKEN). google-auth exchanges it
at Google's STS for an access token of the vallum-deployer service account. No key files exist."""

import json
import os
import tempfile
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_done = {}


def project_id() -> str:
    return os.environ.get("GCP_PROJECT_ID", "vallum-512306")


def setup() -> str:
    """Write the external-account credential config and point Google's libraries at it."""
    if "path" in _done:
        return _done["path"]
    token = os.environ.get("GCP_ID_TOKEN")
    if not token:
        raise SystemExit("GCP_ID_TOKEN is missing: this job needs id_tokens and must run on protected main.")
    provider = os.environ["GCP_WIF_PROVIDER"]
    sa = os.environ["GCP_DEPLOYER_SA"]
    workdir = Path(tempfile.mkdtemp(prefix="vallum-gcp-"))
    token_file = workdir / "oidc_token"
    token_file.write_text(token, encoding="utf-8")
    config = {
        "type": "external_account",
        "audience": f"//iam.googleapis.com/{provider}",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "service_account_impersonation_url":
            f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateAccessToken",
        "credential_source": {"file": str(token_file)},
    }
    cred_path = workdir / "cred.json"
    cred_path.write_text(json.dumps(config), encoding="utf-8")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred_path)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id())
    _done["path"] = str(cred_path)
    return _done["path"]


def credentials():
    import google.auth

    setup()
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds


def session():
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(credentials())


def firestore_client():
    from google.cloud import firestore

    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        from google.auth.credentials import AnonymousCredentials

        return firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "vallum-test"), credentials=AnonymousCredentials())
    return firestore.Client(project=project_id(), credentials=credentials())
