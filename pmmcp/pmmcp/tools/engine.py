"""Engine control tools for Rust binaries (pmengine, pmproxy)."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: "FastMCP") -> None:
    """Register engine control tools with the MCP server."""

    @mcp.tool(
        name="check_engine_status",
        description="""Check if pmengine binary is available.

Returns whether the pmengine Rust binary is built and accessible.
The engine is the HFT trading engine that executes transpiled strategies.""",
    )
    def check_engine_status() -> dict:
        """Check pmengine binary availability.

        Returns:
            Dict with available (bool), path (str), version (str or None)
        """
        # Check if binary is in PATH or target directory
        binary = shutil.which("pmengine")
        if not binary:
            # Check common cargo build locations
            import os

            repo_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            )
            for path in [
                os.path.join(repo_root, "pmengine", "target", "release", "pmengine"),
                os.path.join(repo_root, "pmengine", "target", "debug", "pmengine"),
            ]:
                if os.path.isfile(path):
                    binary = path
                    break

        if not binary:
            return {
                "available": False,
                "path": None,
                "version": None,
                "message": "pmengine not found. Run 'cargo build --release' in pmengine/",
            }

        # Try to get version
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            version = result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            version = None

        return {
            "available": True,
            "path": binary,
            "version": version,
            "message": "pmengine is available",
        }

    @mcp.tool(
        name="check_proxy_status",
        description="""Check if pmproxy binary is available.

Returns whether the pmproxy Rust binary is built and accessible.
The proxy is a reverse proxy for Polymarket APIs with caching and auth.""",
    )
    def check_proxy_status() -> dict:
        """Check pmproxy binary availability.

        Returns:
            Dict with available (bool), path (str), version (str or None)
        """
        binary = shutil.which("pmproxy")
        if not binary:
            import os

            repo_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            )
            for path in [
                os.path.join(repo_root, "pmproxy", "target", "release", "pmproxy"),
                os.path.join(repo_root, "pmproxy", "target", "debug", "pmproxy"),
            ]:
                if os.path.isfile(path):
                    binary = path
                    break

        if not binary:
            return {
                "available": False,
                "path": None,
                "version": None,
                "message": "pmproxy not found. Run 'cargo build --release' in pmproxy/",
            }

        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            version = result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            version = None

        return {
            "available": True,
            "path": binary,
            "version": version,
            "message": "pmproxy is available",
        }

    @mcp.tool(
        name="run_engine_dry_run",
        description="""Run pmengine in dry-run mode for testing.

Executes a dry-run of the trading engine, which simulates order execution
without actually placing trades. Useful for testing strategy transpilation.

Note: Requires pmengine to be built (cargo build --release).""",
    )
    def run_engine_dry_run(
        strategy_file: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict:
        """Run pmengine in dry-run mode.

        Args:
            strategy_file: Optional path to transpiled strategy file
            timeout_seconds: Maximum execution time (default: 30s)

        Returns:
            Dict with success, stdout, stderr, return_code
        """
        # Find binary
        binary = shutil.which("pmengine")
        if not binary:
            import os

            repo_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            )
            for path in [
                os.path.join(repo_root, "pmengine", "target", "release", "pmengine"),
                os.path.join(repo_root, "pmengine", "target", "debug", "pmengine"),
            ]:
                if os.path.isfile(path):
                    binary = path
                    break

        if not binary:
            return {
                "success": False,
                "error": "pmengine not found. Run 'cargo build --release' in pmengine/",
                "stdout": "",
                "stderr": "",
                "return_code": -1,
            }

        # Build command
        cmd = [binary, "--dry-run"]
        if strategy_file:
            cmd.extend(["--strategy", strategy_file])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Timeout after {timeout_seconds} seconds",
                "stdout": "",
                "stderr": "",
                "return_code": -1,
            }
        except OSError as e:
            return {
                "success": False,
                "error": str(e),
                "stdout": "",
                "stderr": "",
                "return_code": -1,
            }
