"""Tests for pmproxy client."""

import os
import pytest
from pmproxy import PmProxy, PROXY_URL


# Skip all tests if PMPROXY_URL is not set to a real endpoint
PROXY_AVAILABLE = os.environ.get("PMPROXY_URL", "").startswith("https://")


@pytest.fixture
def client():
    """Create a client using the proxy."""
    c = PmProxy(proxy=True)
    yield c
    c.close()


@pytest.fixture
def direct_client():
    """Create a client that connects directly to Polymarket."""
    c = PmProxy(proxy=False)
    yield c
    c.close()


class TestConfig:
    """Test configuration loading."""

    def test_proxy_url_from_env(self):
        """PROXY_URL should be loaded from environment."""
        env_url = os.environ.get("PMPROXY_URL")
        if env_url:
            assert PROXY_URL == env_url


@pytest.mark.skipif(not PROXY_AVAILABLE, reason="PMPROXY_URL not configured")
class TestClobProxy:
    """Test CLOB API through proxy."""

    def test_ok(self, client):
        """Health check should return True."""
        assert client.ok() is True

    def test_sampling_markets(self, client):
        """Should return list of markets."""
        markets, raw = client.sampling_markets()
        assert len(markets) > 0
        assert raw is not None
        # Check first market has expected fields
        market = markets[0]
        assert market.condition_id
        assert market.question

    def test_order_book(self, client):
        """Should return order book for a token."""
        markets, _ = client.sampling_markets()
        if markets and markets[0].tokens:
            token_id = markets[0].tokens[0].token_id
            book = client.order_book(token_id)
            assert book.asset_id or book.bids or book.asks

    def test_midpoint(self, client):
        """Should return midpoint price."""
        markets, _ = client.sampling_markets()
        if markets and markets[0].tokens:
            token_id = markets[0].tokens[0].token_id
            mid = client.midpoint(token_id)
            assert isinstance(mid, float)

    def test_spread(self, client):
        """Should return bid, ask, spread."""
        markets, _ = client.sampling_markets()
        if markets and markets[0].tokens:
            token_id = markets[0].tokens[0].token_id
            bid, ask, spread = client.spread(token_id)
            assert isinstance(bid, float)
            assert isinstance(ask, float)
            assert isinstance(spread, float)


@pytest.mark.skipif(not PROXY_AVAILABLE, reason="PMPROXY_URL not configured")
class TestGammaProxy:
    """Test Gamma API through proxy."""

    def test_events(self, client):
        """Should return list of events."""
        events, raw = client.events(limit=5)
        assert len(events) <= 5
        assert raw is not None

    def test_markets(self, client):
        """Should return list of markets."""
        markets, raw = client.markets(limit=5)
        assert len(markets) <= 5

    def test_tags(self, client):
        """Should return list of tags."""
        tags = client.tags()
        assert isinstance(tags, list)


@pytest.mark.skipif(not PROXY_AVAILABLE, reason="PMPROXY_URL not configured")
class TestChainProxy:
    """Test Chain/RPC API through proxy."""

    def test_block_number(self, client):
        """Should return current block number."""
        block = client.block_number()
        assert isinstance(block, int)
        assert block > 0


class TestProxyToggle:
    """Test proxy toggle functionality."""

    def test_default_proxy_false(self):
        """Default should be proxy=False."""
        client = PmProxy()
        assert client.proxy is False
        client.close()

    def test_proxy_true(self):
        """Can set proxy=True."""
        client = PmProxy(proxy=True)
        assert client.proxy is True
        client.close()

    def test_custom_proxy_url(self):
        """Can set custom proxy URL."""
        client = PmProxy(proxy_url="http://custom:8080")
        assert client.proxy_url == "http://custom:8080"
        client.close()

    @pytest.mark.skipif(not PROXY_AVAILABLE, reason="PMPROXY_URL not configured")
    def test_per_request_override(self, client, direct_client):
        """Can override proxy setting per-request."""
        # Client with proxy=True, override to False
        assert client.ok(proxy=False) is True
        # Client with proxy=False, would need proxy=True to go through proxy
        # (only test if direct works)
        assert direct_client.ok(proxy=False) is True
