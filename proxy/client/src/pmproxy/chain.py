"""Chain/RPC API methods for Polygon."""

from typing import Any, Optional

from .base import BaseClient

# Polygon contract addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # CTF Exchange


class ChainMixin:
    """Chain/RPC API methods mixin for Polygon."""

    def _rpc_call(
        self,
        method: str,
        params: list[Any],
        *,
        proxy: Optional[bool] = None,
    ) -> Any:
        """
        Make a JSON-RPC call to Polygon.

        Args:
            method: RPC method name
            params: RPC parameters
            proxy: Override instance proxy setting

        Returns:
            RPC result
        """
        self: BaseClient
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }
        resp = self._post("chain", "", json=payload, proxy=proxy)
        data = resp.json()
        if "error" in data:
            raise Exception(f"RPC error: {data['error']}")
        return data.get("result")

    def block_number(self, *, proxy: Optional[bool] = None) -> int:
        """
        Get the current block number.

        Args:
            proxy: Override instance proxy setting
        """
        result = self._rpc_call("eth_blockNumber", [], proxy=proxy)
        return int(result, 16)

    def usdc_balance(
        self, address: str, *, proxy: Optional[bool] = None
    ) -> float:
        """
        Get USDC balance for an address.

        Args:
            address: Ethereum address (0x...)
            proxy: Override instance proxy setting

        Returns:
            USDC balance (human-readable, 6 decimals)
        """
        # ERC20 balanceOf(address) selector: 0x70a08231
        # Pad address to 32 bytes
        padded_address = address.lower().replace("0x", "").zfill(64)
        call_data = f"0x70a08231{padded_address}"

        result = self._rpc_call(
            "eth_call",
            [{"to": USDC_ADDRESS, "data": call_data}, "latest"],
            proxy=proxy,
        )
        # USDC has 6 decimals
        return int(result, 16) / 1e6

    def token_balance(
        self,
        address: str,
        token_id: str,
        *,
        proxy: Optional[bool] = None,
    ) -> float:
        """
        Get CTF token balance for an address.

        Args:
            address: Ethereum address (0x...)
            token_id: Token ID (numeric string or hex)
            proxy: Override instance proxy setting

        Returns:
            Token balance (human-readable)
        """
        # ERC1155 balanceOf(address, uint256) selector: 0x00fdd58e
        padded_address = address.lower().replace("0x", "").zfill(64)

        # Handle token_id as either decimal string or hex
        if token_id.startswith("0x"):
            token_int = int(token_id, 16)
        else:
            token_int = int(token_id)
        padded_token = hex(token_int)[2:].zfill(64)

        call_data = f"0x00fdd58e{padded_address}{padded_token}"

        result = self._rpc_call(
            "eth_call",
            [{"to": CTF_ADDRESS, "data": call_data}, "latest"],
            proxy=proxy,
        )
        # CTF tokens have no decimals (whole units)
        return int(result, 16)

    def eth_balance(
        self, address: str, *, proxy: Optional[bool] = None
    ) -> float:
        """
        Get native MATIC/POL balance for an address.

        Args:
            address: Ethereum address (0x...)
            proxy: Override instance proxy setting

        Returns:
            Balance in MATIC (18 decimals)
        """
        result = self._rpc_call("eth_getBalance", [address, "latest"], proxy=proxy)
        return int(result, 16) / 1e18
