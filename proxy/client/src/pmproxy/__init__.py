"""pmproxy - Python HTTP client for Polymarket APIs."""

from .client import PmProxy
from .config import PROXY_URL, LAMBDA_URL, CLOB_URL, GAMMA_URL, CHAIN_URL
from .clob import OrderBook, OrderBookLevel, Market, Token
from .gamma import Event, GammaMarket, GammaToken, Tag
from .chain import USDC_ADDRESS, CTF_ADDRESS

__all__ = [
    # Main client
    "PmProxy",
    # Config
    "PROXY_URL",
    "LAMBDA_URL",
    "CLOB_URL",
    "GAMMA_URL",
    "CHAIN_URL",
    # CLOB types
    "OrderBook",
    "OrderBookLevel",
    "Market",
    "Token",
    # Gamma types
    "Event",
    "GammaMarket",
    "GammaToken",
    "Tag",
    # Chain constants
    "USDC_ADDRESS",
    "CTF_ADDRESS",
]

__version__ = "0.1.0"
