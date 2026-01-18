"""Tests for proxy connectivity."""

import os

import pytest
import requests

PROXY_URL = os.environ.get("PMPROXY_URL", "").rstrip("/")

# Check if proxy requires auth (returns 401 on unauthenticated request)
def _proxy_requires_auth() -> bool:
    if not PROXY_URL:
        return False
    try:
        resp = requests.get(f"{PROXY_URL}/clob/", timeout=5)
        return resp.status_code == 401
    except Exception:
        return False

# Check if we have cognito credentials
def _has_cognito_creds() -> bool:
    return bool(
        os.environ.get("PMPROXY_COGNITO_CLIENT_ID")
        and os.environ.get("PMPROXY_USERNAME")
        and os.environ.get("PMPROXY_PASSWORD")
    )

def _get_auth_headers() -> dict[str, str]:
    """Get auth headers if cognito credentials are available."""
    if not _has_cognito_creds():
        return {}
    try:
        from polymarket.cognito import CognitoAuth
        auth = CognitoAuth()
        return auth.get_auth_header()
    except Exception:
        return {}

# Skip if proxy requires auth but we don't have credentials
_skip_no_auth = pytest.mark.skipif(
    PROXY_URL and _proxy_requires_auth() and not _has_cognito_creds(),
    reason="Proxy requires auth but no credentials available"
)


@pytest.mark.skipif(not PROXY_URL, reason="PMPROXY_URL not set")
@_skip_no_auth
class TestProxy:
    """Test API calls through the proxy."""

    @pytest.fixture(autouse=True)
    def auth_headers(self):
        """Get auth headers for authenticated proxy requests."""
        self._headers = _get_auth_headers()

    def test_clob_ok(self):
        """CLOB health check through proxy."""
        resp = requests.get(f"{PROXY_URL}/clob/", headers=self._headers, timeout=10)
        assert resp.status_code == 200
        assert resp.json() == "OK"

    def test_clob_sampling_markets(self):
        """Fetch sampling markets through proxy."""
        resp = requests.get(f"{PROXY_URL}/clob/sampling-markets", headers=self._headers, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert len(data["data"]) > 0
        # Check market structure
        market = data["data"][0]
        assert "tokens" in market
        assert "question" in market

    def test_gamma_events(self):
        """Fetch events through proxy."""
        resp = requests.get(
            f"{PROXY_URL}/gamma/events", params={"limit": 3}, headers=self._headers, timeout=10
        )
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) > 0
        assert "title" in events[0]

    def test_chain_block_number(self):
        """RPC call through proxy."""
        resp = requests.post(
            f"{PROXY_URL}/chain",
            json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            headers=self._headers,
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        # Block number should be a hex string
        block = int(data["result"], 16)
        assert block > 50_000_000  # Polygon is well past this
