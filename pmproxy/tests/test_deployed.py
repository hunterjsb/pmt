"""Post-deploy verification against the live Lambda.

Runs as the last step of `deploy-pmproxy.yml`. Standalone — uses boto3
directly to mint a Cognito access token (no cross-package dependency on
pmtrader). Set these env vars before invoking pytest:

    PMPROXY_URL                  https://...lambda-url.eu-west-1.on.aws
    PMPROXY_COGNITO_REGION       e.g. eu-west-1
    PMPROXY_COGNITO_CLIENT_ID    Cognito App Client ID
    PMPROXY_USERNAME             Cognito username
    PMPROXY_PASSWORD             Cognito password
"""

from __future__ import annotations

import os

import pytest
import requests

PROXY_URL = os.environ.get("PMPROXY_URL", "").rstrip("/")


def _cognito_access_token() -> str | None:
    """Mint a Cognito access token from USER_PASSWORD_AUTH. None if creds missing."""
    required = ("PMPROXY_COGNITO_CLIENT_ID", "PMPROXY_USERNAME", "PMPROXY_PASSWORD")
    if not all(os.environ.get(k) for k in required):
        return None
    try:
        import boto3
    except ImportError:
        return None

    client = boto3.client(
        "cognito-idp",
        region_name=os.environ.get("PMPROXY_COGNITO_REGION", "us-east-1"),
    )
    resp = client.initiate_auth(
        ClientId=os.environ["PMPROXY_COGNITO_CLIENT_ID"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": os.environ["PMPROXY_USERNAME"],
            "PASSWORD": os.environ["PMPROXY_PASSWORD"],
        },
    )
    return resp["AuthenticationResult"]["AccessToken"]


@pytest.fixture(scope="module")
def auth_headers() -> dict[str, str]:
    """Bearer header for the deployed Lambda. {} if no Cognito creds."""
    token = _cognito_access_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


@pytest.mark.skipif(not PROXY_URL, reason="PMPROXY_URL not set")
class TestDeployed:
    """Smoke tests against the live Lambda. Each must pass for the deploy
    to be considered successful."""

    def test_health(self, auth_headers):
        # /health is unauth-gated by design (intentionally bypasses Cognito).
        resp = requests.get(f"{PROXY_URL}/health", timeout=10)
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy"}

    def test_badge(self, auth_headers):
        resp = requests.get(f"{PROXY_URL}/badge", timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("schemaVersion") == 1
        assert body.get("message") == "online"

    def test_metrics(self, auth_headers):
        # /metrics is also auth-free — Prometheus scrapers can't carry a JWT.
        resp = requests.get(f"{PROXY_URL}/metrics", timeout=10)
        assert resp.status_code == 200
        assert "pmproxy_requests_total" in resp.text
        assert resp.headers.get("content-type", "").startswith("text/plain")

    def test_clob_sampling_markets(self, auth_headers):
        resp = requests.get(
            f"{PROXY_URL}/clob/sampling-markets", headers=auth_headers, timeout=10,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "data" in data and len(data["data"]) > 0
        assert "tokens" in data["data"][0] and "question" in data["data"][0]

    def test_gamma_events(self, auth_headers):
        resp = requests.get(
            f"{PROXY_URL}/gamma/events",
            params={"limit": 3},
            headers=auth_headers,
            timeout=10,
        )
        assert resp.status_code == 200, resp.text
        events = resp.json()
        assert len(events) > 0
        assert "title" in events[0]

    def test_chain_block_number(self, auth_headers):
        # Exercises the publicnode upstream swap (was the v0.5.0 fix).
        resp = requests.post(
            f"{PROXY_URL}/chain/",
            json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            headers=auth_headers,
            timeout=10,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "result" in data
        # Polygon is well past block 50M.
        assert int(data["result"], 16) > 50_000_000

    def test_unauthenticated_clob_rejected(self):
        resp = requests.get(f"{PROXY_URL}/clob/sampling-markets", timeout=10)
        assert resp.status_code == 401
        assert resp.json().get("error") == "missing_token"

    def test_unknown_path_404(self, auth_headers):
        resp = requests.get(f"{PROXY_URL}/nonsense", headers=auth_headers, timeout=10)
        assert resp.status_code == 404
