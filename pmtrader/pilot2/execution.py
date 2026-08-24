"""LIVE order placement. Off unless BOTH switches are thrown.

`--live` and `PILOT2_LIVE=1` are both required, because one of them alone is a
mistake somebody could make: a flag survives a copied command line, an env var
survives a unit file. Requiring both means capital moves only where a human
edited two different things.

Credentials come from the environment, or from env FILES named by
`PILOT2_ENV_FILES` (comma-separated, loaded with `override=False` so an
exported var always wins). Nothing here hardcodes a path to a key. On the EU
box that is `/home/ec2-user/.pmt/engine.env` (non-secret knobs) plus
`/home/ec2-user/.pmt/l0.env` (the L0 EOA key, generated on the box and never
copied off it); locally it is the repo `.env`. The split is the point.

NO KEY MATERIAL IS EVER LOGGED. `describe()` is the only thing that reports on
credentials and it reports the funder address (public, on-chain) and the
signature type. `PM_PRIVATE_KEY` is read into a local, handed to the client,
and never enters a record, a message or a repr.

Orders are FAK marketable-limit BUYs at the quoted ask. FAK because the EV
replay's fill model is "at the quoted ask, capped by the quoted ask size" and
FAK is that model's live equivalent — it takes what is there and kills the
rest. A GTC remainder would rest on the book and could fill minutes later
against an edge that has since evaporated, which is a different strategy than
the one that was measured. There is no SELL path at all: positions hold to
resolution.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from polymarket import hosts

LIVE_ENV = "PILOT2_LIVE"
ENV_FILES_ENV = "PILOT2_ENV_FILES"
CLOB_HOST_ENV = "PILOT2_CLOB_HOST"

CHAIN_ID = 137  # Polygon mainnet

# The EU deposit wallet is a CONTRACT that validates orders via EIP-1271; its
# order signature is an ERC-7739 wrapper, not a bare ECDSA blob, and the
# `signer` FIELD of the order holds the wallet rather than the EOA. Type 3 is
# V2-only. See docs/deposit-wallet.md.
DEPOSIT_WALLET_SIG_TYPE = 3


class LiveRefused(RuntimeError):
    """Live mode asked for and not safely available. Fatal, never downgraded."""


def live_enabled(flag: bool, env: dict | None = None) -> bool:
    """BOTH the flag and PILOT2_LIVE=1. Either alone is shadow."""
    env = os.environ if env is None else env
    return bool(flag) and str(env.get(LIVE_ENV, "")).strip() == "1"


def load_env_files(spec: str | None = None) -> list[str]:
    """Load every file named by PILOT2_ENV_FILES. Returns the paths loaded.

    `override=False`: an exported variable always beats a file, so an operator
    can point the pilot at a different wallet for one run without editing
    anything on disk.
    """
    raw = spec if spec is not None else os.environ.get(ENV_FILES_ENV, "")
    loaded = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        path = Path(p).expanduser()
        if not path.is_file():
            continue
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
        loaded.append(str(path))
    return loaded


@dataclass(frozen=True)
class Creds:
    """Everything the client needs EXCEPT the key, which is never stored here."""

    funder: str
    sig_type: int
    host: str

    def describe(self) -> dict:
        """Loggable. Funder is a public on-chain address; the key is absent by
        construction, not by redaction."""
        return {"funder": self.funder, "sig_type": self.sig_type, "host": self.host}


def read_creds(env: dict | None = None) -> Creds:
    """Validate the credential environment WITHOUT holding the key.

    Refuses type 3 with no funder: a deposit wallet has no derivation from the
    EOA the way a proxy or Safe does, so it must be given or authentication
    fails later and less clearly.
    """
    env = os.environ if env is None else env
    if not env.get("PM_PRIVATE_KEY"):
        raise LiveRefused("PM_PRIVATE_KEY is not set — no signer, no live mode.")
    funder = (env.get("PM_FUNDER_ADDRESS") or "").strip()
    try:
        sig_type = int(env.get("PM_SIGNATURE_TYPE", "3"))
    except ValueError as e:
        raise LiveRefused(f"PM_SIGNATURE_TYPE is not an integer: {e}") from None
    if sig_type == DEPOSIT_WALLET_SIG_TYPE and not funder:
        raise LiveRefused(
            "PM_SIGNATURE_TYPE=3 (deposit wallet) with no PM_FUNDER_ADDRESS. "
            "The wallet is a contract and has no derivation from the EOA."
        )
    host = (env.get(CLOB_HOST_ENV) or hosts.CLOB).rstrip("/")
    return Creds(funder=funder, sig_type=sig_type, host=host)


def build_client(env: dict | None = None):
    """An authenticated py_clob_client_v2 client. Imported lazily so shadow
    mode never touches the trading SDK at all.

    Deliberately NOT `polymarket.clob_v2.create_authenticated_clob_v2`: that
    one requires PMPROXY_URL and routes through the SigV4 Lambda, which would
    put a US egress back in front of the EU box and undo the only reason that
    box exists.
    """
    env = os.environ if env is None else env
    creds = read_creds(env)
    from py_clob_client_v2 import ClobClient

    client = ClobClient(
        host=creds.host,
        chain_id=CHAIN_ID,
        key=env["PM_PRIVATE_KEY"],   # local only; never stored, never logged
        signature_type=creds.sig_type,
        funder=creds.funder,
    )
    client.set_api_creds(client.create_or_derive_api_key())
    return client


@dataclass(frozen=True)
class OrderPlan:
    """A buy, fully specified, before anything leaves the process."""

    slug: str
    side: str            # the OUTCOME side, "up"/"down" — the order is always a BUY
    token: str
    price: float
    shares: float

    def record(self) -> dict:
        return {"slug": self.slug, "side": self.side, "token": self.token,
                "price": self.price, "shares": round(self.shares, 4)}


def place(client, plan: OrderPlan, tick_size: str | None = None) -> dict:
    """Send one FAK marketable-limit BUY. Returns the exchange's answer.

    BUY only. There is no sell path in this module and adding one would be
    adding an exit policy the measured strategy does not have.
    """
    from py_clob_client_v2 import (
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )

    ts = tick_size or client.get_tick_size(plan.token)
    return client.create_and_post_order(
        order_args=OrderArgs(token_id=plan.token, price=plan.price,
                             side=Side.BUY, size=plan.shares),
        options=PartialCreateOrderOptions(tick_size=ts),
        order_type=OrderType.FAK,
    )


def filled_shares(ack: object) -> float:
    """Shares actually taken, from an order ack. 0.0 when the ack says nothing.

    A FAK that took nothing is not an error and not a fill; treating an
    unparseable ack as a full fill would overstate exposure, and understating
    it is the safe direction only because the (slug, side) is marked fired
    BEFORE the send — the clip is spent either way and cannot be retried.
    """
    if not isinstance(ack, dict):
        return 0.0
    for key in ("takingAmount", "sizeMatched", "size_matched"):
        v = ack.get(key)
        if isinstance(v, (int, float, str)):
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                continue
    return 0.0
