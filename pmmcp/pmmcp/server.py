"""Main MCP server entry point for PMT ecosystem."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastmcp import FastMCP

# Load env vars at startup (for proxy auth, etc.)
# DOTENV_PATH can point to a custom .env location
load_dotenv(os.environ.get("DOTENV_PATH"))

from polymarket import AuthenticatedClob, Clob, Gamma, create_authenticated_clob

from .reflect import register_class_methods

# Create the MCP server
mcp = FastMCP(
    name="pmt-mcp",
    instructions="""PMT (Polymarket Trading Toolkit) MCP Server.

Provides access to:
- Market data (order books, prices, events, search) - no auth required
- Trading operations (orders, positions, balances) - requires PM_PRIVATE_KEY
- Scanners for finding trading opportunities
- Backtest capabilities for strategy evaluation

Read-only tools work without authentication.
Trading tools require PM_PRIVATE_KEY and PM_FUNDER_ADDRESS environment variables.
""",
)

# ============================================================================
# Lazy instance factories
# ============================================================================


def _use_proxy() -> bool:
    """Check if proxy should be used (PMPROXY_URL is set)."""
    return bool(os.environ.get("PMPROXY_URL"))


def _create_clob() -> Clob:
    """Create read-only CLOB client.

    Note: We don't use proxy for read-only Clob because py_clob_client
    doesn't support custom auth headers. Cloudflare mainly blocks trading,
    not read operations.
    """
    return Clob(proxy=False)


def _create_gamma() -> Gamma:
    """Create Gamma API client.

    Note: We don't use proxy for Gamma because some endpoints like /search
    require Polymarket's own cookies/auth which our proxy can't provide.
    """
    return Gamma(proxy=False)


def _create_authenticated_clob() -> AuthenticatedClob:
    """Create authenticated CLOB client from environment.

    Uses proxy if PMPROXY_URL is set.

    Raises:
        RuntimeError: If required environment variables are missing
    """
    from dotenv import load_dotenv

    # Load from DOTENV_PATH if specified, otherwise default .env
    dotenv_path = os.environ.get("DOTENV_PATH")
    load_dotenv(dotenv_path)

    private_key = os.environ.get("PM_PRIVATE_KEY")
    funder_address = os.environ.get("PM_FUNDER_ADDRESS")

    if not private_key or not funder_address:
        raise RuntimeError(
            "Trading operations require PM_PRIVATE_KEY and PM_FUNDER_ADDRESS "
            "environment variables. Set these in your .env file."
        )

    client = create_authenticated_clob(proxy=_use_proxy())
    if client is None:
        raise RuntimeError("Failed to create authenticated client")
    return client


# ============================================================================
# Auto-register SDK class methods
# ============================================================================

# Register Clob (read-only market data) methods
# These work without authentication
_clob_tools = register_class_methods(
    mcp,
    Clob,
    _create_clob,
    prefix="",
    exclude={"ok", "server_time"},  # Internal health checks
)

# Register Gamma (market metadata) methods
_gamma_tools = register_class_methods(
    mcp,
    Gamma,
    _create_gamma,
    prefix="gamma_",
)

# Register AuthenticatedClob (trading) methods
# Exclude methods that duplicate Clob functionality
_auth_tools = register_class_methods(
    mcp,
    AuthenticatedClob,
    _create_authenticated_clob,
    prefix="",
    exclude={
        # Duplicates from Clob
        "ok",
        "order_book",
        "midpoint",
        "price",
        "spread",
        # Internal helpers
        "_rpc_call",
        "_get_headers",
    },
)

# ============================================================================
# Manual tool registrations
# ============================================================================

# Import and register scanner tools
from .tools import scanners

scanners.register(mcp)

# Import and register backtest tools
from .tools import backtest

backtest.register(mcp)

# Import and register engine control tools
from .tools import engine

engine.register(mcp)


# ============================================================================
# Entry point
# ============================================================================


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
