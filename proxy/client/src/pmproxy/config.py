"""Configuration for pmproxy client."""

import os

# Direct Polymarket API URLs
CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
CHAIN_URL = "https://polygon-rpc.com"

# Proxy URLs - set via environment or override in client
PROXY_URL = os.environ.get("PMPROXY_URL", "http://localhost:8080")
LAMBDA_URL = os.environ.get("PMPROXY_LAMBDA_URL", "")
