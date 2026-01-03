"""Main pmproxy client combining all API methods."""

from typing import Optional

from .base import BaseClient
from .clob import ClobMixin
from .gamma import GammaMixin
from .chain import ChainMixin


class PmProxy(BaseClient, ClobMixin, GammaMixin, ChainMixin):
    """
    HTTP client for Polymarket APIs via pmproxy.

    Can route requests directly to Polymarket or through a proxy server
    (EC2, Lambda, etc.) for geo-restrictions or latency optimization.

    Example:
        # Direct to Polymarket (no proxy)
        client = PmProxy()
        markets, _ = client.sampling_markets()

        # Through proxy by default
        client = PmProxy(proxy=True, proxy_url="http://my-proxy:8080")
        events, _ = client.events(limit=10)

        # Override per-request
        book = client.order_book(token_id, proxy=False)  # Direct
        price = client.midpoint(token_id, proxy=True)    # Via proxy

    Environment Variables:
        PMPROXY_URL: Default proxy URL (EC2, ECS, VPS, etc.)
        PMPROXY_LAMBDA_URL: Lambda function URL for serverless proxy
    """

    def __init__(
        self,
        *,
        proxy: bool = False,
        proxy_url: Optional[str] = None,
        lambda_url: Optional[str] = None,
        timeout: float = 30.0,
    ):
        """
        Initialize the client.

        Args:
            proxy: If True, route requests through proxy by default
            proxy_url: Override PMPROXY_URL environment variable
            lambda_url: Override PMPROXY_LAMBDA_URL environment variable
            timeout: Request timeout in seconds
        """
        super().__init__(
            proxy=proxy,
            proxy_url=proxy_url,
            lambda_url=lambda_url,
            timeout=timeout,
        )
