#!/bin/sh
# Prints a short-lived Google access token for vallum-deployer, exchanged from this job's GitLab
# OIDC token (id_tokens: GCP_ID_TOKEN) through Workload Identity Federation. No key files exist.
set -eu
: "${GCP_ID_TOKEN:?this job needs id_tokens and must run on protected main}"

STS=$(curl -sf https://sts.googleapis.com/v1/token \
  --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  --data-urlencode "audience=//iam.googleapis.com/${GCP_WIF_PROVIDER}" \
  --data-urlencode "scope=https://www.googleapis.com/auth/cloud-platform" \
  --data-urlencode "requested_token_type=urn:ietf:params:oauth:token-type:access_token" \
  --data-urlencode "subject_token_type=urn:ietf:params:oauth:token-type:jwt" \
  --data-urlencode "subject_token=${GCP_ID_TOKEN}" | jq -r '.access_token // empty')
[ -n "$STS" ] || { echo "STS token exchange failed" >&2; exit 1; }

TOKEN=$(curl -sf -X POST -H "Authorization: Bearer ${STS}" -H "Content-Type: application/json" \
  "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${GCP_DEPLOYER_SA}:generateAccessToken" \
  -d '{"scope":["https://www.googleapis.com/auth/cloud-platform"]}' | jq -r '.accessToken // empty')
[ -n "$TOKEN" ] || { echo "service account impersonation failed" >&2; exit 1; }
echo "$TOKEN"
