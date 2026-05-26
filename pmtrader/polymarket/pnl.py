"""Realized P&L from Polymarket activity history.

Replays the per-user activity stream into a per-asset ledger and emits a
realized-P&L event for every disposal (SELL or REDEEM). REWARD/YIELD are
treated as pure income (no cost basis).

Definitions
-----------
- Realized P&L = (proceeds - cost basis) on disposal events that already
  happened. SELLs use the trade price; REDEEMs use usdcSize/size as the
  per-share payoff (typically $1 for winners, $0 for losers — but losers
  rarely emit REDEEM events; they just expire worthless).
- Unrealized P&L = (current mark - avg cost) * size for positions still open.
- "Expired worthless" = ledger shares > 0 for an asset that's no longer in
  current positions. These are realized losses at unknown timestamp; bucketed
  as all-time only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class LedgerEntry:
    asset: str
    condition_id: str
    title: str
    shares: float = 0.0
    avg_cost: float = 0.0


@dataclass
class RealizedEvent:
    ts: int
    asset: str | None
    condition_id: str
    title: str
    kind: str  # "SELL", "REDEEM", "EXPIRED", "REWARD", "YIELD"
    pnl: float
    proceeds: float = 0.0
    cost: float = 0.0


@dataclass
class ReplayResult:
    ledger: dict[str, LedgerEntry] = field(default_factory=dict)
    realized: list[RealizedEvent] = field(default_factory=list)
    income: list[RealizedEvent] = field(default_factory=list)


def replay_activity(activity: list[dict]) -> ReplayResult:
    """Process activity oldest→newest into a position ledger + realized events.

    `activity` is the raw data-api `/activity` response (any order — we sort).
    """
    res = ReplayResult()
    asset_by_condition: dict[str, list[str]] = defaultdict(list)

    for ev in sorted(activity, key=lambda e: e.get("timestamp", 0)):
        t = ev.get("type")
        ts = int(ev.get("timestamp", 0))
        cid = ev.get("conditionId") or ""
        title = ev.get("title") or ""

        if t == "TRADE":
            asset = ev["asset"]
            size = float(ev["size"])
            usdc = float(ev.get("usdcSize", 0))
            # `price` in /activity is rounded to the displayed tick; usdcSize
            # is authoritative. Sweeps across price levels surface as one
            # event with the headline price.
            per_share = (usdc / size) if size > 0 else float(ev.get("price", 0))
            side = ev.get("side")
            L = res.ledger.get(asset)
            if L is None:
                L = LedgerEntry(asset=asset, condition_id=cid, title=title)
                res.ledger[asset] = L
                asset_by_condition[cid].append(asset)
            if side == "BUY":
                new_shares = L.shares + size
                if new_shares > 0:
                    L.avg_cost = (L.shares * L.avg_cost + usdc) / new_shares
                L.shares = new_shares
            elif side == "SELL":
                consumed = min(size, L.shares) if L.shares > 0 else size
                pnl = (per_share - L.avg_cost) * consumed
                L.shares -= consumed
                res.realized.append(RealizedEvent(
                    ts=ts, asset=asset, condition_id=cid, title=title,
                    kind="SELL", pnl=pnl,
                    proceeds=per_share * consumed, cost=L.avg_cost * consumed,
                ))

        elif t == "REDEEM":
            usdc = float(ev.get("usdcSize", 0))
            size = float(ev.get("size", 0))
            # REDEEM's `asset` is blank; find what we held at this condition.
            candidates = [a for a in asset_by_condition.get(cid, []) if res.ledger[a].shares > 0]
            if size <= 0 or usdc <= 0:
                # Resolution marker for a losing market — write off everything
                # we still hold under this condition_id as expired worthless.
                for asset in asset_by_condition.get(cid, []):
                    L = res.ledger[asset]
                    if L.shares <= 0:
                        continue
                    cost_consumed = L.avg_cost * L.shares
                    res.realized.append(RealizedEvent(
                        ts=ts, asset=asset, condition_id=cid, title=L.title or title,
                        kind="EXPIRED", pnl=-cost_consumed,
                        proceeds=0.0, cost=cost_consumed,
                    ))
                    L.shares = 0.0
            elif candidates:
                # Pick the largest holding (usually only one outcome held).
                asset = max(candidates, key=lambda a: res.ledger[a].shares)
                L = res.ledger[asset]
                consumed = min(size, L.shares)
                payoff_per_share = usdc / size
                pnl = (payoff_per_share - L.avg_cost) * consumed
                cost_consumed = L.avg_cost * consumed
                L.shares -= consumed
                res.realized.append(RealizedEvent(
                    ts=ts, asset=asset, condition_id=cid, title=L.title or title,
                    kind="REDEEM", pnl=pnl,
                    proceeds=payoff_per_share * consumed, cost=cost_consumed,
                ))
            else:
                # No basis tracked — count proceeds as windfall (history may
                # predate our captured activity window).
                res.realized.append(RealizedEvent(
                    ts=ts, asset=None, condition_id=cid, title=title,
                    kind="REDEEM", pnl=usdc, proceeds=usdc, cost=0.0,
                ))

        elif t == "REWARD":
            res.income.append(RealizedEvent(
                ts=ts, asset=None, condition_id=cid, title=title,
                kind="REWARD", pnl=float(ev.get("usdcSize", 0)),
                proceeds=float(ev.get("usdcSize", 0)),
            ))
        elif t == "YIELD":
            res.income.append(RealizedEvent(
                ts=ts, asset=None, condition_id="", title="",
                kind="YIELD", pnl=float(ev.get("usdcSize", 0)),
                proceeds=float(ev.get("usdcSize", 0)),
            ))

    return res


def reconcile_expired(result: ReplayResult, current_positions: list[dict]) -> None:
    """Write off ledger shares that aren't in current positions as expired losses.

    Mutates `result.realized` in place. Timestamp is 0 (= bucketed as all-time only).
    """
    held = {p.get("asset") for p in current_positions if float(p.get("size", 0)) > 0}
    for asset, L in list(result.ledger.items()):
        if L.shares <= 0:
            continue
        if asset in held:
            continue
        cost = L.avg_cost * L.shares
        if cost <= 0:
            continue
        result.realized.append(RealizedEvent(
            ts=0, asset=asset, condition_id=L.condition_id, title=L.title,
            kind="EXPIRED", pnl=-cost, proceeds=0.0, cost=cost,
        ))
        L.shares = 0.0


def bucket_by_window(
    events: list[RealizedEvent],
    now_ts: int,
    windows_seconds: dict[str, int | None],
) -> dict[str, float]:
    """Sum `pnl` per window. None window = all-time (includes ts=0)."""
    totals = {name: 0.0 for name in windows_seconds}
    for e in events:
        for name, span in windows_seconds.items():
            if span is None or (e.ts > 0 and now_ts - e.ts <= span):
                totals[name] += e.pnl
    return totals
