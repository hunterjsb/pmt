"""Authenticated py_clob_client_v2 ClobClient with proxy + SigV4 support.

Strategies use this so they don't each repeat the monkey-patch + L1/L2 setup.

The pmproxy Lambda sits behind an AWS_IAM Function URL, so every proxied CLOB
call must be SigV4-signed (see sigv4.py). py_clob_client_v2 has no native hook
for this, so we monkey-patch its HTTP helper. Idempotent — safe to import from
multiple strategies.

Also persists CLOB API credentials to ~/.cache/pmt/ so warm starts skip the
/clob/auth/api-key round-trip (creds are deterministic per private_key/chain).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .sigv4 import _install_sigv4_patch_once

CHAIN_ID = 137  # Polygon mainnet


def _creds_cache_path(funder: str) -> Path:
    cache_dir = Path.home() / ".cache" / "pmt"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"clob_creds_{funder.lower()}_{CHAIN_ID}.json"


def _load_cached_creds(funder: str):
    """Return cached ApiCreds or None."""
    from py_clob_client_v2 import ApiCreds

    path = _creds_cache_path(funder)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return ApiCreds(
            api_key=data["api_key"],
            api_secret=data["api_secret"],
            api_passphrase=data["api_passphrase"],
        )
    except (KeyError, json.JSONDecodeError):
        return None


def _save_creds(funder: str, creds) -> None:
    path = _creds_cache_path(funder)
    path.write_text(
        json.dumps(
            {
                "api_key": creds.api_key,
                "api_secret": creds.api_secret,
                "api_passphrase": creds.api_passphrase,
            }
        )
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


def create_authenticated_clob_v2():
    """Build a fully-authenticated v2 ClobClient from env vars.

    Reads PMPROXY_URL, PM_PRIVATE_KEY, PM_FUNDER_ADDRESS, PM_SIGNATURE_TYPE.
    Installs the Cognito monkey-patch on first call. Caches CLOB API creds to
    disk so subsequent invocations skip the L1 derive call.
    """
    from py_clob_client_v2 import ClobClient

    _install_sigv4_patch_once()

    from . import hosts
    proxy_url = hosts.proxy_url() + "/clob"
    if proxy_url == "/clob":
        raise RuntimeError("PMPROXY_URL not set")
    pk = os.environ["PM_PRIVATE_KEY"]
    funder = os.environ["PM_FUNDER_ADDRESS"]
    sig_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))

    client = ClobClient(
        host=proxy_url,
        chain_id=CHAIN_ID,
        key=pk,
        signature_type=sig_type,
        funder=funder,
    )

    creds = _load_cached_creds(funder)
    if creds is None:
        creds = client.create_or_derive_api_key()
        _save_creds(funder, creds)

    client.set_api_creds(creds)
    return client
