"""Where fleet trouble goes: the mubs attention channel, or a command of your own.

Hunter already has an alerting surface he trusts and does not mute by accident
— mubs' attention registry, which dedupes, re-nudges once a day, and reroutes
(never drops) under mute mode. A second pager competing with it would be one
more thing to silence. So the doctor is a CLIENT of that channel and owns none
of it.

The invocation path is deliberately the thinnest one that exists
----------------------------------------------------------------
`mubslib.attention.notify()` is a wrapper around a single async Lambda invoke.
We issue that invoke directly instead of importing mubslib, for three reasons:
the EU box has no mubs checkout and no Discord token; the pmtrader venv should
not grow a dependency on another project's library to report its own health;
and the payload is a four-key JSON document that has been stable since the
worker started sending it. If mubs ever changes it, this breaks loudly in one
place.

    {"action": "raise",   "service": "pmt-fleet", "detail": "...", "detected_by": "..."}
    {"action": "resolve", "service": "pmt-fleet"}

Mute is applied by the attention Lambda, not by callers, so routing through it
respects mute mode by construction — a muted page lands in `#muted-reports`
instead of a DM, and nothing is lost. Two things need a decision on the mubs
side before this goes loud, both flagged in the report: `pmt-fleet` is not in
`attention.SERVICES` (so the card renders the generic label), and it is not in
`mute.EXEMPT` (so a quiet evening reroutes it).

What is NOT here, and why
-------------------------
There is no post to the `#status` board. That board is one message the mubs
gateway Lambda edits in place on its own tick, built from state it reads out of
the mubs table; there is no external "append a line" API, and inventing one by
writing rows into someone else's table is exactly the coupling the brief
forbids. Getting fleet health onto the board is a mubs-side change (a
`_board_fleet_line`), proposed in the report. Until then the low-key surface is
`--check`'s stdout and the resolve of an open attention.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

SERVICE = "pmt-fleet"
ATTENTION_FN = "mubs-attention"
# mubs runs in us-east-1; the pmt fleet table is eu-west-1. Two clients, no
# shared region assumption — writing this down because a copied region
# constant is how a page silently goes nowhere.
ATTENTION_REGION = os.environ.get("MUBS_ATTENTION_REGION", "us-east-1")

DETAIL_MAX = 200  # the attention Lambda truncates here; do it ourselves so logs match

LATCH_PATH = Path.home() / ".pmt" / "fleet" / "paged.json"


class Notifier:
    """One page channel, chosen by name.

    `dry` is the default on purpose. A doctor that pages the moment it is first
    run is a doctor nobody runs twice; bring-up wants to see the message it
    WOULD have sent.
    """

    def __init__(self, backend: str = "dry", *, cmd: str | None = None, log=print):
        self.backend = backend
        self.cmd = cmd
        self.log = log

    # -- the two verbs -----------------------------------------------------
    def page(self, detail: str, *, node: str = "") -> bool:
        """Raise (or refresh) the fleet attention. True iff it actually left.

        The return value is load-bearing and not decoration: the latch below
        may only be set on a page that was accepted. Latching a failed send is
        how you get a silent outage — mubs learned that the expensive way over
        a 56-hour window in August, and the lesson transfers unchanged.
        """
        detail = detail.strip()[:DETAIL_MAX]
        payload = {
            "action": "raise",
            "service": SERVICE,
            "detail": detail,
            "detected_by": f"pmt-fleet-doctor@{node}" if node else "pmt-fleet-doctor",
        }
        return self._send(payload, human=f"PAGE {detail}")

    def resolve(self, *, node: str = "") -> bool:
        """Clear an open fleet attention — the fleet is healthy again."""
        payload = {"action": "resolve", "service": SERVICE}
        return self._send(payload, human="RESOLVE fleet healthy")

    # -- backends ----------------------------------------------------------
    def _send(self, payload: dict, *, human: str) -> bool:
        if self.backend == "dry":
            self.log(f"[notify:dry] {human} | {json.dumps(payload)}")
            return False
        if self.backend == "cmd":
            return self._send_cmd(payload, human=human)
        if self.backend == "mubs":
            return self._send_mubs(payload, human=human)
        self.log(f"[notify] unknown backend {self.backend!r} — nothing sent")
        return False

    def _send_cmd(self, payload: dict, *, human: str) -> bool:
        """Hand the payload to an operator-supplied command on stdin.

        The escape hatch, and the reason this module is not a hard dependency
        on mubs' internals: anything that can read JSON on stdin is a valid
        pager. Non-zero exit means the page did not leave.
        """
        if not self.cmd:
            self.log("[notify] backend=cmd but no --notify-cmd given")
            return False
        try:
            proc = subprocess.run(
                shlex.split(self.cmd),
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"[notify] --notify-cmd failed: {type(e).__name__}: {e}")
            return False
        if proc.returncode != 0:
            self.log(f"[notify] --notify-cmd exit {proc.returncode}: {proc.stderr.strip()[:200]}")
            return False
        return True

    def _send_mubs(self, payload: dict, *, human: str) -> bool:
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:
            self.log("[notify] boto3 unavailable — page not sent")
            return False
        try:
            client = boto3.client("lambda", region_name=ATTENTION_REGION)
            resp = client.invoke(
                FunctionName=ATTENTION_FN,
                InvocationType="Event",  # fire-and-forget, like mubslib does
                Payload=json.dumps(payload).encode(),
            )
        except (ClientError, BotoCoreError) as e:
            self.log(f"[notify] mubs-attention invoke failed: {type(e).__name__}: {e}")
            return False
        ok = 200 <= int(resp.get("StatusCode", 0)) < 300
        if not ok:
            self.log(f"[notify] mubs-attention returned {resp.get('StatusCode')}")
        return ok


# --- the latch -------------------------------------------------------------
# "Have we already paged for this?" kept out of the pmt-fleet table on purpose:
# the table is the safety substrate and every extra writer is another way to
# throttle it at the worst moment. mubs-attention dedupes on its own side
# anyway (one DM, then at most one re-nudge per 20h), so this latch is about
# not spamming the invoke, not about not spamming Hunter.


def read_latch(path: Path = LATCH_PATH) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # Unreadable reads as ALREADY PAGED — never spam on a disk blip. The
        # inverse (unreadable reads as "not yet paged") turns one bad read into
        # a page every cadence.
        return {"paged": True, "unreadable": True}


def write_latch(paged: bool, *, detail: str = "", path: Path = LATCH_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"paged": bool(paged), "detail": detail, "at": time.time()}))
    except OSError:
        pass  # best-effort: a latch we cannot persist costs a duplicate invoke, nothing more
