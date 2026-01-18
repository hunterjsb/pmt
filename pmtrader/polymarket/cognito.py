"""AWS Cognito authentication for pmproxy.

This module provides token acquisition and caching for Cognito JWT tokens
used to authenticate with pmproxy when multi-tenant auth is enabled.

Environment Variables:
    PMPROXY_COGNITO_CLIENT_ID: Cognito App Client ID
    PMPROXY_USERNAME: Cognito username
    PMPROXY_PASSWORD: Cognito password
    PMPROXY_COGNITO_REGION: AWS region (default: us-east-1)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError


@dataclass
class CognitoToken:
    """Cached Cognito token with expiration."""

    access_token: str
    id_token: str
    refresh_token: str | None
    expires_at: float  # Unix timestamp


class CognitoAuth:
    """Cognito authentication client with token caching.

    Acquires and caches JWT tokens from AWS Cognito using USER_PASSWORD_AUTH flow.
    Tokens are automatically refreshed when they expire.

    Example:
        >>> auth = CognitoAuth()
        >>> token = auth.get_token()
        >>> headers = {"Authorization": f"Bearer {token}"}
    """

    def __init__(
        self,
        client_id: str | None = None,
        username: str | None = None,
        password: str | None = None,
        region: str | None = None,
    ) -> None:
        """Initialize Cognito auth client.

        Args:
            client_id: Cognito App Client ID (or PMPROXY_COGNITO_CLIENT_ID env var)
            username: Cognito username (or PMPROXY_USERNAME env var)
            password: Cognito password (or PMPROXY_PASSWORD env var)
            region: AWS region (or PMPROXY_COGNITO_REGION env var, default: us-east-1)
        """
        self.client_id = client_id or os.environ.get("PMPROXY_COGNITO_CLIENT_ID", "")
        self.username = username or os.environ.get("PMPROXY_USERNAME", "")
        self.password = password or os.environ.get("PMPROXY_PASSWORD", "")
        self.region = region or os.environ.get("PMPROXY_COGNITO_REGION", "us-east-1")

        if not self.client_id:
            raise ValueError(
                "Cognito client_id required (set PMPROXY_COGNITO_CLIENT_ID)"
            )
        if not self.username:
            raise ValueError("Cognito username required (set PMPROXY_USERNAME)")
        if not self.password:
            raise ValueError("Cognito password required (set PMPROXY_PASSWORD)")

        self._client = boto3.client("cognito-idp", region_name=self.region)
        self._token: CognitoToken | None = None

        # Buffer time before expiry to refresh (5 minutes)
        self._refresh_buffer = 300

    def _is_token_valid(self) -> bool:
        """Check if cached token is still valid (not expired)."""
        if self._token is None:
            return False
        # Refresh if within buffer time of expiry
        return time.time() < (self._token.expires_at - self._refresh_buffer)

    def _authenticate(self) -> CognitoToken:
        """Authenticate with Cognito using USER_PASSWORD_AUTH flow."""
        try:
            response = self._client.initiate_auth(
                ClientId=self.client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": self.username,
                    "PASSWORD": self.password,
                },
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            raise AuthenticationError(f"Cognito auth failed: {error_code} - {error_msg}") from e

        result = response.get("AuthenticationResult", {})

        access_token = result.get("AccessToken")
        id_token = result.get("IdToken")
        refresh_token = result.get("RefreshToken")
        expires_in = result.get("ExpiresIn", 3600)

        if not access_token or not id_token:
            raise AuthenticationError("Cognito response missing tokens")

        return CognitoToken(
            access_token=access_token,
            id_token=id_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in,
        )

    def _refresh_token(self) -> CognitoToken:
        """Refresh the token using the refresh token."""
        if self._token is None or self._token.refresh_token is None:
            return self._authenticate()

        try:
            response = self._client.initiate_auth(
                ClientId=self.client_id,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={
                    "REFRESH_TOKEN": self._token.refresh_token,
                },
            )
        except ClientError:
            # If refresh fails, fall back to full authentication
            return self._authenticate()

        result = response.get("AuthenticationResult", {})

        access_token = result.get("AccessToken")
        id_token = result.get("IdToken")
        expires_in = result.get("ExpiresIn", 3600)

        if not access_token or not id_token:
            # Fall back to full authentication
            return self._authenticate()

        # Keep the existing refresh token (refresh response doesn't include new one)
        return CognitoToken(
            access_token=access_token,
            id_token=id_token,
            refresh_token=self._token.refresh_token,
            expires_at=time.time() + expires_in,
        )

    def get_token(self, token_type: str = "access") -> str:
        """Get a valid JWT token, refreshing if necessary.

        Args:
            token_type: Type of token to return ("access" or "id")

        Returns:
            JWT token string
        """
        if not self._is_token_valid():
            if self._token is not None and self._token.refresh_token:
                self._token = self._refresh_token()
            else:
                self._token = self._authenticate()

        if token_type == "id":
            return self._token.id_token
        return self._token.access_token

    def get_auth_header(self, token_type: str = "access") -> dict[str, str]:
        """Get Authorization header with Bearer token.

        Args:
            token_type: Type of token to use ("access" or "id")

        Returns:
            Dict with Authorization header
        """
        token = self.get_token(token_type)
        return {"Authorization": f"Bearer {token}"}

    def clear_cache(self) -> None:
        """Clear the cached token, forcing re-authentication on next request."""
        self._token = None


class AuthenticationError(Exception):
    """Raised when Cognito authentication fails."""

    pass


def create_cognito_auth() -> CognitoAuth | None:
    """Create a CognitoAuth instance from environment variables.

    Returns:
        CognitoAuth instance if all required env vars are set, None otherwise.
    """
    try:
        return CognitoAuth()
    except ValueError:
        return None
