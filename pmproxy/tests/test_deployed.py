"""Post-deploy verification against the live Lambda.

Runs as the last step of `deploy-pmproxy.yml`. The Function URL is
`AuthType=AWS_IAM` — AuthType NONE 403s account-wide — so **every** request
here is SigV4-signed, including the ones the proxy itself treats as public
(`/health`, `/badge`, `/metrics`). IAM gates the door; pmproxy's own auth is
off (`PMPROXY_AUTH_ENABLED=false`) and Cognito Bearer is retired, because a
Bearer token and a SigV4 signature both want the `Authorization` header.

Credentials come from the ambient chain: the OIDC deploy role in CI, your
profile locally. Env:

    PMPROXY_URL           https://...lambda-url.eu-west-1.on.aws
    PMPROXY_AWS_REGION    signing region (default eu-west-1)
"""

from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import urlencode

import pytest
import requests

PROXY_URL = os.environ.get("PMPROXY_URL", "").rstrip("/")
REGION = os.environ.get("PMPROXY_AWS_REGION", "eu-west-1")


def _frozen_credentials():
    import boto3

    creds = boto3.Session().get_credentials()
    if creds is None:
        pytest.fail("no AWS credentials — the Function URL requires SigV4")
    return creds.get_frozen_credentials()


def call(method: str, path: str, *, params=None, body=None, sign=True, timeout=15):
    """Request the deployed proxy, SigV4-signed unless `sign=False`."""
    url = f"{PROXY_URL}{path}"
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)

    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {}
    if body is not None:
        headers["content-type"] = "application/json"

    if sign:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        req = AWSRequest(method=method, url=url, data=payload, headers=dict(headers))
        # Function URLs reject a signed request that omits this header with the
        # same bare 403 they give an unsigned one. It is not optional.
        req.headers["X-Amz-Content-SHA256"] = hashlib.sha256(payload).hexdigest()
        SigV4Auth(_frozen_credentials(), "lambda", REGION).add_auth(req)
        headers = dict(req.headers)

    return requests.request(
        method, url, headers=headers, data=payload or None, timeout=timeout
    )


@pytest.mark.skipif(not PROXY_URL, reason="PMPROXY_URL not set")
class TestDeployed:
    """Smoke tests against the live Lambda. Each must pass for the deploy
    to be considered successful."""

    def test_health(self):
        resp = call("GET", "/health")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "healthy"}

    def test_badge(self):
        resp = call("GET", "/badge")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("schemaVersion") == 1
        assert body.get("message") == "online"

    def test_metrics(self):
        resp = call("GET", "/metrics")
        assert resp.status_code == 200, resp.text
        assert "pmproxy_requests_total" in resp.text
        assert resp.headers.get("content-type", "").startswith("text/plain")

    def test_clob_sampling_markets(self):
        resp = call("GET", "/clob/sampling-markets")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "data" in data and len(data["data"]) > 0
        assert "tokens" in data["data"][0] and "question" in data["data"][0]

    def test_gamma_events(self):
        resp = call("GET", "/gamma/events", params={"limit": 3})
        assert resp.status_code == 200, resp.text
        events = resp.json()
        assert len(events) > 0
        assert "title" in events[0]

    def test_chain_block_number(self):
        # Exercises the publicnode upstream swap (was the v0.5.0 fix).
        resp = call(
            "POST",
            "/chain/",
            body={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "result" in data
        # Polygon is well past block 50M.
        assert int(data["result"], 16) > 50_000_000

    def test_unsigned_request_rejected(self):
        # The 403 comes from AWS, not pmproxy — the request never reaches the
        # function. This is the whole security model now that Cognito is gone.
        resp = call("GET", "/clob/sampling-markets", sign=False)
        assert resp.status_code == 403, resp.text

    def test_unknown_path_404(self):
        resp = call("GET", "/nonsense")
        assert resp.status_code == 404, resp.text
