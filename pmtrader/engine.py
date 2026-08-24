"""Client for the locally-running `pmengine` control plane.

The engine binds an HTTP control plane on `127.0.0.1:7531` by default;
override with `PMENGINE_CONTROL_URL`. This module is the only place that
talks to it — everything else (CLI, future scripts) imports from here.

Three styles of call, deliberately separate:

- `get(path)` / `post(path, body)` — strict: prints + exits on failure.
  Use when the user is asking the engine for something explicit and a
  non-response is a real error (`pmt engine status`, alert approval).
- `notify(path, body)` / `fetch(path)` — best-effort write and read.
  Return None when the engine is unreachable instead of exiting. Use for
  "tell the engine if it's listening" handoffs (registering external
  orders, scheduling TTL cancels) and for polled reads that must not
  print a red error every cadence when a tunnel blinks (the watch
  dashboard's remote tape).
- `place(side, token, price, size)` / `place_or_direct(...)` — orderpath:
  route writes through the engine to share its rate-limit budget. The
  `_or_direct` variant falls back to a direct CLOB place when the
  engine isn't running, then notifies the engine after the fact.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import click
import requests
from rich.console import Console

from polymarket import errlog

console = Console()


def base_url() -> str:
    return os.environ.get("PMENGINE_CONTROL_URL", "http://127.0.0.1:7531").rstrip("/")


# ---------- strict GET/POST (exit on error) ----------

def get(path: str) -> Any:
    try:
        r = requests.get(f"{base_url()}{path}", timeout=5)
        r.raise_for_status()
    except requests.ConnectionError:
        console.print(
            f"[red]Cannot reach pmengine at {base_url()}.[/red] "
            "Is the engine running? Check `ps -ef | grep pmengine`."
        )
        sys.exit(1)
    except requests.HTTPError as e:
        console.print(f"[red]Engine returned {e.response.status_code}: {e.response.text}[/red]")
        sys.exit(1)
    return r.json()


def post(path: str, body: dict | None = None) -> Any:
    try:
        r = requests.post(f"{base_url()}{path}", json=body, timeout=10)
    except requests.ConnectionError:
        console.print(f"[red]Cannot reach pmengine at {base_url()}.[/red]")
        sys.exit(1)
    if r.status_code >= 400:
        try:
            msg = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        except Exception:
            msg = r.text
        console.print(f"[red]HTTP {r.status_code}: {msg}[/red]")
        sys.exit(1)
    return r.json()


# ---------- best-effort read/notify (silent on error) ----------

def fetch(path: str, params: dict | None = None, timeout: float = 3.0) -> Any | None:
    """Best-effort GET — the read counterpart to notify().

    Returns None on anything short of a 2xx instead of exiting, and prints
    NOTHING. A caller on a repeating cadence (the watch dashboard polling the
    remote tape over an SSM tunnel) would otherwise paint a red error into the
    operator's terminal every few seconds for a link that reconnects on its
    own. Strict `get()` stays the right call when the operator asked a
    question and a non-answer is the answer.

    Prints nothing, but no longer says nothing: the marks go to errlog, where a
    tunnel that has been down for an hour looks different from one that
    blinked. This is the call behind the watch dashboard's tape panel, and a
    panel that quietly stops advancing is indistinguishable from a fleet with
    nothing to say.
    """
    try:
        r = requests.get(f"{base_url()}{path}", params=params, timeout=timeout)
        if r.status_code >= 400:
            errlog.note("engine.fetch.http", RuntimeError(f"HTTP {r.status_code}"),
                        path=path, body=r.text[:200])
            return None
        return r.json()
    except (requests.RequestException, ValueError) as e:
        errlog.note("engine.fetch", e, path=path)
        return None


def notify(path: str, body: dict | None = None) -> dict | None:
    """Returns the JSON response on success, or None if unreachable / 4xx.

    Best-effort by design and marked by errlog anyway — a notify that has
    silently failed every time for a day is a control-plane outage nobody was
    told about.
    """
    try:
        r = requests.post(f"{base_url()}{path}", json=body, timeout=3)
        if r.status_code >= 400:
            errlog.note("engine.notify.http", RuntimeError(f"HTTP {r.status_code}"),
                        path=path, body=r.text[:200])
            return None
        return r.json()
    except (requests.ConnectionError, requests.Timeout) as e:
        errlog.note("engine.notify", e, path=path)
        return None


# ---------- order routing ----------

def place(side: str, token: str, price: float, size: int) -> dict | None:
    """Ask the engine to place the order on our behalf.

    Routing CLI writes through the engine puts every account-touching call
    on a single queue, so the engine's pollers/strategies and the CLI no
    longer race for the account-wide ~5 req/sec budget. The engine also
    handles tick rounding from its cache, so this path skips the CLI's
    separate tick_size REST lookup.

    Returns a dict shaped like a direct place response on success, None if
    the engine is unreachable (caller should fall back to direct). On a 4xx
    rejection (risk limit, validation error) this prints the error and exits
    — a deliberate engine rejection is NOT a reason to silently bypass it.
    """
    try:
        r = requests.post(
            f"{base_url()}/trade/place",
            json={"token_id": token, "side": side, "price": str(price), "size": str(size)},
            timeout=20,
        )
    except (requests.ConnectionError, requests.Timeout):
        return None
    if r.status_code >= 400:
        try:
            msg = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        except Exception:
            msg = r.text
        console.print(f"[red]engine rejected order: {msg}[/red]")
        sys.exit(1)
    body = r.json()
    return {"success": True, "orderID": body.get("order_id"), "via_engine": True}


def place_or_direct(side: str, *, token: str, price: float, size: int, tick: str | None) -> dict:
    """Try engine first; on unreachable, fall back to direct CLOB.

    The engine path serializes against the account-wide budget and skips the
    CLI's tick_size lookup; the direct path keeps `pmt buy/sell` working when
    the engine isn't running. `--tick` short-circuits to direct since the
    engine ignores caller-supplied ticks (it always uses its cached lookup).
    """
    from polymarket import PolymarketAPI

    if tick is None:
        resp = place(side, token, price, size)
        if resp is not None:
            return resp
        console.print("[dim](engine unreachable; placing direct)[/dim]")
    api = PolymarketAPI()
    resp = api.place(side, token=token, price=price, size=size, tick_size=tick)
    register_external(resp, token=token, side=side, price=price, size=size)
    return resp


def register_external(resp: dict, *, token: str, side: str, price: float, size: int) -> None:
    """After a direct (non-engine) place, tell the engine so its
    `/orders/all` view stays unified. Silent on engine offline."""
    if not isinstance(resp, dict) or not resp.get("success"):
        return
    order_id = resp.get("orderID") or resp.get("order_id")
    if not order_id:
        return
    notify(
        "/orders/external",
        {
            "id": order_id,
            "token_id": token,
            "side": side,
            "price": str(price),
            "size": str(size),
            "source": "pmt-cli",
        },
    )


# ---------- TTL helpers ----------

import re

_TTL_TOKEN = re.compile(r"(\d+)([dhms])", re.IGNORECASE)
_TTL_UNIT_SECS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_ttl(value: str) -> int:
    """Parse '30m', '2h', '1h30m', '2d' → seconds. Raises click.BadParameter on bad input."""
    parts = _TTL_TOKEN.findall(value)
    if not parts or "".join(f"{n}{u}" for n, u in parts).lower() != value.lower():
        raise click.BadParameter(
            f"--ttl: invalid duration '{value}' (use forms like '30m', '2h', '1h30m', '2d')"
        )
    return sum(int(n) * _TTL_UNIT_SECS[u.lower()] for n, u in parts)


def schedule_cancel(resp: dict, *, ttl_seconds: int) -> None:
    """After a successful place, ask the engine to cancel after `ttl_seconds`.

    The order itself is on the book whether or not the engine is up; if the
    engine is unreachable, warn loudly because the TTL will not be honored.
    """
    if not isinstance(resp, dict) or not resp.get("success"):
        return
    order_id = resp.get("orderID") or resp.get("order_id")
    if not order_id:
        return
    result = notify(f"/orders/{order_id}/schedule-cancel", {"after_seconds": ttl_seconds})
    if result is None:
        console.print(
            f"[red]WARNING: engine unreachable — TTL of {ttl_seconds}s NOT registered. "
            f"Order is placed but will rest until manually cancelled.[/red]"
        )
    else:
        console.print(f"[dim]TTL: cancel scheduled at {result.get('at')}[/dim]")
