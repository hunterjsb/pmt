"""Base HTTP client with proxy toggle."""

from typing import Any, Optional
import httpx

from . import config


class BaseClient:
    """Base HTTP client that handles proxy routing."""

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
            proxy_url: Override PROXY_URL (EC2, ECS, VPS, etc.)
            lambda_url: Override LAMBDA_URL (serverless runtime)
            timeout: Request timeout in seconds
        """
        self.proxy = proxy
        self.proxy_url = (proxy_url or config.PROXY_URL).rstrip("/")
        self.lambda_url = (lambda_url or config.LAMBDA_URL).rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _resolve_proxy(self, proxy: Optional[bool]) -> bool:
        """Resolve proxy setting: explicit param > instance default."""
        return proxy if proxy is not None else self.proxy

    def _get_base_url(self, service: str, *, proxy: Optional[bool] = None) -> str:
        """
        Get the base URL for a service.

        Args:
            service: One of 'clob', 'gamma', 'chain'
            proxy: Override instance proxy setting

        Returns:
            Base URL to use for requests
        """
        use_proxy = self._resolve_proxy(proxy)

        if use_proxy:
            # Use proxy URL with service prefix
            base = self.proxy_url
            return f"{base}/{service}"
        else:
            # Direct to Polymarket APIs
            if service == "clob":
                return config.CLOB_URL
            elif service == "gamma":
                return config.GAMMA_URL
            elif service == "chain":
                return config.CHAIN_URL
            else:
                raise ValueError(f"Unknown service: {service}")

    def _get(
        self,
        service: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        proxy: Optional[bool] = None,
        **kwargs,
    ) -> httpx.Response:
        """
        Make a GET request.

        Args:
            service: One of 'clob', 'gamma', 'chain'
            path: API path (without leading slash)
            params: Query parameters
            proxy: Override instance proxy setting
        """
        base_url = self._get_base_url(service, proxy=proxy)
        url = f"{base_url}/{path}" if path else base_url
        return self._client.get(url, params=params, **kwargs)

    def _post(
        self,
        service: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        proxy: Optional[bool] = None,
        **kwargs,
    ) -> httpx.Response:
        """
        Make a POST request.

        Args:
            service: One of 'clob', 'gamma', 'chain'
            path: API path (without leading slash)
            json: JSON body
            proxy: Override instance proxy setting
        """
        base_url = self._get_base_url(service, proxy=proxy)
        url = f"{base_url}/{path}" if path else base_url
        return self._client.post(url, json=json, **kwargs)
