"""The poll loop — feeds in, decision out, one clip per window-side, ever.

Shape of one pass, per watched window:

    ref/spot/sigma/banked   (stream, in memory)   -> terminal_p_up
    two asks + their sizes  (CLOB REST, 2s)       -> devig -> blended_p_up
    edge = p_side - ask - taker_fee(ask)          -> fire iff >= MIN_EDGE
    risk.refuse(...)                              -> a NAMED law, or None
    shadow: write the would-be trade   |   live: send one FAK buy

Shadow and live keep SEPARATE risk books. A shadow window must obey every law
the live one does — that is what makes the tape a faithful record of what the
pilot would have done — but a shadow position must never consume live budget.

Positions are never sold. A live clip is written to the redeem queue as a
CANDIDATE the moment it is booked — before the order leaves — and again as DUE
when `retire_settled` releases its exposure at the settlement grace; the tokens
themselves are swept by hand (see README) because pmtrader has no relayer
batch-redeem path and inventing one for a $40 book would be building a
money-moving code path nobody has reviewed. The candidate row exists because
the settlement row is written 300s after the close by a process that may not
still be running: a position that filled and then lost its process was
invisible to the sweep.

The live risk book itself is rebuilt from `live-tape.jsonl` on startup
(`Pilot.rehydrate`), so a restart cannot forget which window-sides are already
spent and buy one of them again.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from . import books, execution, policy, predict, risk, state, windows
from . import series as series_mod

# The settlement grace before a filled window's exposure is released and it is
# queued for the sweep. Matches the outcomes corpus's own grace: settlement
# needs a moment to land on-chain and at data-api.
SETTLE_GRACE_S = 300.0

MODE_SHADOW = "shadow"
MODE_LIVE = "live"


@dataclass
class WindowStats:
    """Counters for one window, flushed as a `window` record at close. The
    tape does NOT get a line per poll per token: at 2s over ~7 series that is
    600k lines a day to answer a question two integers answer."""

    slug: str
    series: str
    mode: str
    polls: int = 0
    priced: int = 0        # model produced a number
    two_sided: int = 0     # the book had an opinion too
    ev_pass: int = 0       # a side cleared the cost model
    fired: int = 0         # a clip was actually taken
    refused: dict[str, int] = field(default_factory=dict)
    # why a poll produced no price. `no_reference_print` dominating a window is
    # the COLD-START signature, not a fault: the reference is the TWAP print at
    # window START, so a pilot that came up mid-window cannot price that window
    # at all and picks up at the next boundary.
    unpriced: dict[str, int] = field(default_factory=dict)
    best_edge: float = float("-inf")
    # last decision-legal (model, book) pair — the blend-fit sample
    calib: tuple[float, float] | None = None

    def refuse(self, reason: str) -> None:
        self.refused[reason] = self.refused.get(reason, 0) + 1

    def unprice(self, why: str | None) -> None:
        k = why or "unknown"
        self.unpriced[k] = self.unpriced.get(k, 0) + 1

    def record(self, end: float) -> dict:
        return {
            "ev": state.EV_WINDOW, "slug": self.slug, "series": self.series,
            "mode": self.mode, "end": end, "polls": self.polls, "priced": self.priced,
            "two_sided": self.two_sided, "ev_pass": self.ev_pass, "fired": self.fired,
            "refused": self.refused, "unpriced": self.unpriced,
            "best_edge": None if self.best_edge == float("-inf") else round(self.best_edge, 5),
        }


class Pilot:
    """One process, two books, no escalation."""

    def __init__(self, *, home, live: bool = False,
                 live_series: list[str] | None = None,
                 shadow_series: list[str] | None = None,
                 stream=None, poller=None, cache=None,
                 clob_client=None, log=print, min_edge: float = policy.MIN_EDGE) -> None:
        self.home = state.ensure_home(home)
        self.live = bool(live)
        self.log = log
        self.min_edge = min_edge
        # In live mode the pilot series trade; the majors are shadowed either
        # way, because their whole purpose is a graded out-of-sample record.
        self.live_series = list(live_series or [])
        self.shadow_series = list(shadow_series if shadow_series is not None
                                  else series_mod.shadow_series())
        if not self.live:
            # Not live: the pilot series are still watched, just on paper.
            for s in self.live_series:
                if s not in self.shadow_series:
                    self.shadow_series.append(s)
            self.live_series = []
        self.stream = stream
        self.poller = poller or books.BookPoller()
        self.cache = cache or windows.WindowCache()
        self.clob = clob_client
        self.shadow_risk = risk.RiskBook(home=self.home)
        self.live_risk = risk.RiskBook(home=self.home)
        self._stats: dict[str, WindowStats] = {}
        self._w = policy.W_SEED
        self._w_source = policy.W_SOURCE_SEED
        self._w_rows = 0
        self._w_mtime = 0.0
        self.polls = 0
        self.refresh_weight()
        self.rehydrated = self.rehydrate()

    # ---- coming back up -------------------------------------------------

    def rehydrate(self, now: float | None = None) -> int:
        """Rebuild the LIVE risk book from the order tape. Returns rows read.

        `RiskBook` lives in memory, so a restart used to come back with an
        empty one: the escalation ban forgot every (slug, side) it had already
        fired, and a window still open could be bought a SECOND time — the
        exact -9.48% RoN shape §1.1 prices. Exposure forgot its positions with
        it, so the $40 cap read as fully free.

        The tape is the authority because it is written BEFORE the send
        (`_fire_live`): a clip that reached the tape is a clip that was spent,
        whether or not the ack came back. Only windows inside their settlement
        grace are restored as POSITIONS — anything older was already retired
        and queued — while the fired keys of any window that has not ended yet
        are what actually keep the one-clip law true across a restart.

        Shadow keeps no book across a restart on purpose: paper exposure that
        outlives the process would seize the $40 shadow budget at boot and stop
        producing the record the pilot exists to produce.
        """
        if not self.live:
            return 0
        now = time.time() if now is None else now
        rows = 0
        for r in state.iter_records(state.LIVE_TAPE, self.home, evs=(state.EV_ORDER,)):
            slug, side = r.get("slug"), r.get("side")
            end = r.get("end")
            if not (isinstance(slug, str) and side in ("up", "down")
                    and isinstance(end, (int, float))):
                continue
            if now >= float(end) + SETTLE_GRACE_S:
                continue    # already retired and queued by the process that ran it
            shares = float(r.get("shares") or 0.0)
            ask = float(r.get("ask") or r.get("price") or 0.0)
            self.live_risk.record_fill(
                slug, side, shares=shares, notional=float(r.get("notional") or shares * ask),
                ask=ask, end=float(end), token=str(r.get("token") or ""),
                t=float(r.get("t") or now))
            rows += 1
        if rows:
            self.log(f"rehydrated {rows} live position(s) from {state.LIVE_TAPE} "
                     f"(${self.live_risk.exposure_used:.2f} exposure, "
                     f"{len(self.live_risk.fired)} spent window-side(s))")
            state.append(state.LIVE_TAPE,
                         {"ev": state.EV_REHYDRATE, "rows": rows,
                          "exposure": round(self.live_risk.exposure_used, 4),
                          "fired": len(self.live_risk.fired)}, self.home)
        return rows

    # ---- the blend weight ------------------------------------------------

    def refresh_weight(self) -> None:
        """Re-read the fitted weight if the grader has rewritten it.

        The FIT lives in the grader, not here: it can only be computed from
        RESOLVED windows, which is exactly what makes it walk-forward — a row
        cannot exist until its window has settled, so no weight is ever fitted
        on a row it is later scored on.
        """
        p = state.path(state.BLEND_WEIGHT, self.home)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return
        if mtime == self._w_mtime:
            return
        d = state.read_json(state.BLEND_WEIGHT, self.home)
        w = d.get("w")
        if isinstance(w, (int, float)) and 0.0 <= float(w) <= 1.0:
            self._w = float(w)
            self._w_source = str(d.get("source") or policy.W_SOURCE_FIT)
            self._w_rows = int(d.get("rows") or 0)
            self._w_mtime = mtime

    @property
    def weight(self) -> dict:
        return {"w": self._w, "w_source": self._w_source, "w_rows": self._w_rows}

    # ---- the pass --------------------------------------------------------

    def watched(self) -> list[tuple[str, str]]:
        """[(series, mode)] — every series this process is looking at."""
        return ([(s, MODE_SHADOW) for s in self.shadow_series]
                + [(s, MODE_LIVE) for s in self.live_series])

    def model_p_up(self, w: windows.Window, now: float) -> tuple[float, dict]:
        """P(up) from the stream alone, plus the inputs, for the tape.

        Returns nan when any input the spec requires is missing. There is no
        substitution path: a window with no reference print at its start, or a
        spot print older than 5s, is a window this model does not have an
        opinion about, and inventing one is the failure mode the whole
        settlement-rule finding is about.
        """
        inputs = {"ref": None, "spot": None, "spot_age_s": None,
                  "sigma_s": None, "n_banked": 0, "why": None}
        if self.stream is None:
            inputs["why"] = "no_stream"
            return float("nan"), inputs
        ref = self.stream.reference(w.symbol, w.start)
        if ref is None:
            inputs["why"] = "no_reference_print"
            return float("nan"), inputs
        inputs["ref"] = ref
        sp = self.stream.spot(w.symbol, now)
        if sp is None:
            inputs["why"] = "spot_stale"
            return float("nan"), inputs
        spot, age = sp
        inputs["spot"], inputs["spot_age_s"] = spot, round(age, 3)
        sigma = self.stream.sigma(w.symbol, w.start, now)
        if not math.isfinite(sigma) or sigma <= 0.0:
            inputs["why"] = "no_sigma"
            return float("nan"), inputs
        inputs["sigma_s"] = sigma
        banked = self.stream.banked(w.symbol, w.end, now)
        inputs["n_banked"] = len(banked)
        p = predict.terminal_p_up(ref, spot, sigma, banked, now, w.end)
        if not math.isfinite(p):
            inputs["why"] = "model_nan"
        return p, inputs

    def poll_once(self, now: float | None = None) -> int:
        """One full pass. Returns the number of clips taken (0 or more).

        The HALT check is FIRST and unconditional. A kill file that appeared
        between two passes must stop this pass, not the next one.
        """
        now = time.time() if now is None else now
        self.polls += 1
        if risk.halted(self.home):
            return -1
        self.refresh_weight()
        taken = 0
        for ser, mode in self.watched():
            w = self.cache.current(ser, now)
            if w is None:
                continue
            taken += self._poll_window(w, mode, now)
        self._flush_closed(now)
        self._retire(now)
        self.cache.sweep(now)
        return taken

    def _stats_for(self, w: windows.Window, mode: str) -> WindowStats:
        st = self._stats.get(w.slug)
        if st is None:
            st = self._stats[w.slug] = WindowStats(slug=w.slug, series=w.series, mode=mode)
        return st

    def _poll_window(self, w: windows.Window, mode: str, now: float) -> int:
        st = self._stats_for(w, mode)
        st.polls += 1
        model_p, inputs = self.model_p_up(w, now)
        if not math.isfinite(model_p):
            st.unprice(inputs["why"])
            return 0
        st.priced += 1

        up, dn = self.poller.top(w.token_up), self.poller.top(w.token_down)
        book_p_up, blend_p_up, decisions = policy.evaluate(
            model_p, up.ask, dn.ask, up.ask_size, dn.ask_size, self._w, self.min_edge)
        if math.isfinite(book_p_up):
            st.two_sided += 1
            # The blend-fit sample: the last moment an entry was still legal.
            # Sampled once per WINDOW, not once per clip — L34's lesson is
            # that a calibration fit must be window-level or it double-counts
            # the windows that happened to be evaluated most often.
            if w.end - now > risk.NO_ENTRY_FINAL_S:
                st.calib = (model_p, book_p_up)
        if not decisions:
            return 0

        book = self.shadow_risk if mode == MODE_SHADOW else self.live_risk
        taken = 0
        for d in decisions:
            st.best_edge = max(st.best_edge, d.edge)
            if not d.fire:
                continue
            st.ev_pass += 1
            reason = book.refuse(w.slug, d.side, w.end, now, ask=d.ask)
            sizing = None if reason else risk.size_clip(d.ask, d.ask_size, book.exposure_used)
            if sizing is None and reason is None:
                reason = risk.R_NO_SIZE
            base = self._base_record(w, mode, d, model_p, book_p_up, blend_p_up, inputs, up, dn, now)
            if reason is not None:
                st.refuse(reason)
                state.append(state.SHADOW_TAPE,
                             {**base, "ev": state.EV_REFUSED, "refused": reason}, self.home)
                continue
            rec = {**base, "shares": round(sizing.shares, 4),
                   "notional": round(sizing.notional, 4), "capped_by": sizing.capped_by}
            if mode == MODE_LIVE:
                taken += self._fire_live(w, d, sizing, rec, book)
            else:
                book.record_fill(w.slug, d.side, shares=sizing.shares,
                                 notional=sizing.notional, ask=d.ask, end=w.end,
                                 token=w.token_up if d.side == "up" else w.token_down, t=now)
                state.append(state.SHADOW_TAPE,
                             {**rec, "ev": state.EV_SHADOW, "would_trade": True}, self.home)
                taken += 1
            st.fired += 1
        return taken

    def _base_record(self, w, mode, d, model_p, book_p_up, blend_p_up, inputs, up, dn, now) -> dict:
        """Everything the grader and a later weight-fit need, on ONE line.

        Deliberately includes the model and book estimators SEPARATELY as well
        as the blend: a tape that only records the blend cannot re-fit the
        weight it was blended with, which is the one parameter the report says
        must not be frozen.
        """
        return {
            "t": round(now, 3), "mode": mode, "slug": w.slug, "series": w.series,
            "symbol": w.symbol, "dur_s": w.dur_s, "start": w.start, "end": w.end,
            "elapsed_frac": round(w.elapsed_frac(now), 4),
            "side": d.side, "token": w.token_up if d.side == "up" else w.token_down,
            "ref": inputs["ref"], "spot": inputs["spot"], "spot_age_s": inputs["spot_age_s"],
            "sigma_s": inputs["sigma_s"], "n_banked": inputs["n_banked"],
            "model_p_up": model_p,
            "book_p_up": None if not math.isfinite(book_p_up) else book_p_up,
            "book_up_ask": None if not math.isfinite(up.ask) else up.ask,
            "book_dn_ask": None if not math.isfinite(dn.ask) else dn.ask,
            "book_up_ask_sz": up.ask_size, "book_dn_ask_sz": dn.ask_size,
            "blend_p_up": blend_p_up, "p_side": d.p_side,
            **self.weight,
            "ask": d.ask, "ask_sz": d.ask_size, "fee": round(d.fee, 6),
            "edge": round(d.edge, 6), "min_edge": self.min_edge,
        }

    def _fire_live(self, w, d, sizing, rec, book) -> int:
        """Place one real clip. Books the (slug, side) as fired BEFORE the send.

        That order is the escalation ban's teeth: if the send raises, if the
        ack is unparseable, if the process dies mid-flight — the clip is
        spent. A retry path here is how a one-clip rule becomes a five-clip
        window, and §1.1 prices that at -9.48% RoN.
        """
        if risk.halted(self.home):
            state.append(state.LIVE_TAPE,
                         {**rec, "ev": state.EV_REFUSED, "refused": risk.R_HALT}, self.home)
            return 0
        plan = execution.OrderPlan(slug=w.slug, side=d.side,
                                   token=w.token_up if d.side == "up" else w.token_down,
                                   price=d.ask, shares=sizing.shares)
        book.record_fill(w.slug, d.side, shares=sizing.shares, notional=sizing.notional,
                         ask=d.ask, end=w.end, token=plan.token)
        state.append(state.LIVE_TAPE, {**rec, "ev": state.EV_ORDER, **plan.record()}, self.home)
        # The redeem queue gets its candidate HERE, before the order leaves —
        # not at settlement. `_retire` is the only thing that used to write the
        # queue, and it runs 300s after the close: a process that died in
        # between left a filled position with nothing anywhere saying it needed
        # sweeping. Writing early can only ever queue a position that turns out
        # not to have filled, which the sweep sees and skips; writing late can
        # lose one, which is money left on the chain.
        state.append(state.REDEEM_QUEUE,
                     {"ev": state.EV_REDEEM_CANDIDATE, "slug": w.slug, "side": d.side,
                      "token": plan.token, "shares": sizing.shares,
                      "notional": sizing.notional, "ask": d.ask, "end": w.end}, self.home)
        try:
            ack = execution.place(self.clob, plan)
        except Exception as e:  # noqa: BLE001 — a failed send is a spent clip, not a crash
            state.append(state.LIVE_TAPE,
                         {"ev": state.EV_ERROR, "slug": w.slug, "side": d.side,
                          "error": f"{type(e).__name__}: {e}"}, self.home)
            self.log(f"live order failed {w.slug} {d.side}: {type(e).__name__}: {e}")
            return 0
        got = execution.filled_shares(ack)
        state.append(state.LIVE_TAPE,
                     {"ev": state.EV_ACK, "slug": w.slug, "side": d.side,
                      "requested": round(sizing.shares, 4), "filled": round(got, 4),
                      "price": d.ask, "ack": _ack_summary(ack)}, self.home)
        self.log(f"live clip {w.slug} {d.side} {got:.2f}/{sizing.shares:.2f} sh @ {d.ask}")
        return 1

    # ---- window lifecycle ------------------------------------------------

    def _flush_closed(self, now: float) -> None:
        """Write the per-window summary and the blend-fit sample at close."""
        for slug in [s for s in self._stats if series_mod.window_bounds(s) and
                     now >= series_mod.window_bounds(s)[1]]:
            st = self._stats.pop(slug)
            end = series_mod.window_bounds(slug)[1]
            state.append(state.SHADOW_TAPE, st.record(end), self.home)
            if st.calib is not None:
                m, b = st.calib
                state.append(state.CALIB,
                             {"ev": state.EV_CALIB, "slug": slug, "series": st.series,
                              "end": end, "model_p_up": m, "book_p_up": b}, self.home)

    def _retire(self, now: float) -> None:
        """Release settled exposure and queue live positions for the sweep."""
        # Paper inventory needs no sweep — the grader reads the tape — but its
        # exposure still has to be released or the shadow book seizes at $40
        # and stops producing the record this pilot exists to produce.
        self.shadow_risk.retire_settled(now, SETTLE_GRACE_S)
        for p in self.live_risk.retire_settled(now, SETTLE_GRACE_S):
            state.append(state.REDEEM_QUEUE, {"ev": state.EV_REDEEM_DUE, **p}, self.home)

    # ---- the loop --------------------------------------------------------

    def run(self, stop: threading.Event, interval_s: float = books.POLL_INTERVAL_S) -> int:
        state.append(state.SHADOW_TAPE,
                     {"ev": state.EV_START, "mode": MODE_LIVE if self.live else MODE_SHADOW,
                      "shadow_series": self.shadow_series, "live_series": self.live_series,
                      **self.weight}, self.home)
        self.log(f"pilot2 up: shadow={','.join(self.shadow_series) or '-'} "
                 f"live={','.join(self.live_series) or '-'} w={self._w:.2f} ({self._w_source})")
        rc = 0
        while not stop.is_set():
            started = time.time()
            try:
                if self.poll_once(started) < 0:
                    state.append(state.SHADOW_TAPE, {"ev": state.EV_HALT}, self.home)
                    self.log(f"HALT file present ({risk.halt_path(self.home)}) — stopping. "
                             "Filled positions ride to resolution.")
                    break
            except Exception as e:  # noqa: BLE001 — one bad pass must not end the pilot
                state.append(state.SHADOW_TAPE,
                             {"ev": state.EV_ERROR, "error": f"{type(e).__name__}: {e}"}, self.home)
                self.log(f"poll failed: {type(e).__name__}: {e}")
            stop.wait(max(0.0, interval_s - (time.time() - started)))
        state.append(state.SHADOW_TAPE,
                     {"ev": state.EV_STOP, "polls": self.polls,
                      "book_requests": getattr(self.poller, "requests_made", None),
                      "book_failures": getattr(self.poller, "failures", None)}, self.home)
        return rc


def _ack_summary(ack: object) -> dict:
    """The few ack fields worth keeping. Never the whole payload — an ack can
    echo order fields, and the tape is not the place to accumulate anything
    that came near a signature."""
    if not isinstance(ack, dict):
        return {}
    return {k: ack.get(k) for k in ("success", "status", "errorMsg", "orderID", "orderId")
            if ack.get(k) is not None}
