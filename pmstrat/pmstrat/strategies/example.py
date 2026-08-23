"""In-tree example strategy — the public documentation of the pmstrat DSL.

Deliberately inert: its one token id is a placeholder that matches no real
market, and the body below is structurally incapable of emitting anything but
Hold — no Buy, no Sell, no order ever reaches the wire. It exists so a public
clone (no pmt-strategies submodule) still has a registered strategy proving
the transpile -> registry -> engine plumbing end to end, and so `pmstrat
transpile --all` always has at least one Python source to chew on.

The shape to copy for a real strategy:
  * `@strategy(...)` names the strategy, its tokens, and its tick cadence;
  * `on_tick(ctx)` returns a list of signals — `ctx.book(token)` is the live
    order book (None until the feed has seen the token), and the early
    `if book is None: return signals` unwrap is the canonical guard;
  * return `Hold()` when there is nothing to do, never raise.
"""

from pmstrat import Hold, strategy

# 64 zero-hex-chars, the shape of a CLOB token id that cannot exist on-chain.
PLACEHOLDER_TOKEN = "0000000000000000000000000000000000000000000000000000000000000000"


@strategy(
    name="example",
    tokens=[PLACEHOLDER_TOKEN],
    tick_interval_ms=60000,
)
def example(ctx):
    """Hold forever — the full on_tick shape with no trading surface.

    The underscore on `_book` is deliberate: the placeholder token never has
    a live book, so the binding only demonstrates the unwrap guard and would
    otherwise trip the unused-variable lint in the transpiled Rust.
    """
    signals = []
    _book = ctx.book("0000000000000000000000000000000000000000000000000000000000000000")
    if _book is None:
        return signals
    signals.append(Hold())
    return signals
