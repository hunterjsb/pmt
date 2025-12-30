"""Environment variable configuration for Polymarket API access.

Create a .env file in the project root with your credentials:

For read-only access (viewing markets, order books):
    (no credentials required)

For authenticated trading:
    PM_PRIVATE_KEY=0x...        # Your Ethereum private key (with 0x prefix)
    PM_FUNDER_ADDRESS=0x...     # Your Ethereum wallet address (with 0x prefix)
    PM_SIGNATURE_TYPE=0         # 0 for EOA, 1 for Poly Proxy, 2 for EIP-1271

Make sure you have USDC on the Polygon network for trading.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Legacy API credentials (optional)
PM_BUILDER_NAME = os.getenv("PM_BUILDER_NAME")
PM_API_KEY = os.getenv("PM_API_KEY")
PM_SECRET = os.getenv("PM_SECRET")
PM_PASSPHRASE = os.getenv("PM_PASSPHRASE")

# Trading credentials (required for placing orders)
PM_PRIVATE_KEY = os.getenv("PM_PRIVATE_KEY")  # Ethereum private key
PM_FUNDER_ADDRESS = os.getenv("PM_FUNDER_ADDRESS")  # Ethereum wallet address
PM_SIGNATURE_TYPE = int(
    os.getenv("PM_SIGNATURE_TYPE", "1")
)  # 0=EOA, 1=Poly Proxy (default, used by web UI), 2=EIP-1271
