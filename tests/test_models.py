"""Unit tests for order book analysis and scanner logic."""

from polymarket.models import Market, OrderBook, OrderBookLevel, Token
from strategies.scanner import analyze_order_book


def test_volume_cliff_detection():
    """Test that we detect a volume cliff when dollar value jumps significantly."""
    market = Market(question="Will this happen?", tokens=[])
    token = Token(outcome="Yes", price=0.93, token_id="test123")

    # Create order book with a clear cliff:
    # Thin levels: $100 @ 93¢, $150 @ 94¢
    # Then a THICK level: $3000 @ 96¢ (this is the cliff!)
    order_book = OrderBook(
        name="Test",
        bids=[],
        asks=[
            OrderBookLevel(price=0.93, size=100.0 / 0.93),  # ~$107 worth
            OrderBookLevel(price=0.94, size=150.0 / 0.94),  # ~$159 worth
            OrderBookLevel(price=0.96, size=3000.0 / 0.96),  # ~$3125 worth - CLIFF!
        ],
    )

    opp = analyze_order_book(
        market,
        token,
        order_book,
        min_volume_jump=1000.0,  # Looking for $1000+ jumps
        min_price_gap_cents=1.0,
    )

    assert opp is not None, "Should detect volume cliff opportunity"
    assert len(opp.buy_levels) == 2, "Should identify 2 thin levels to buy"
    assert opp.cliff_level[0] == 0.96, "Should identify cliff at 96¢"
    assert opp.dollar_value_jump > 1000.0, "Dollar jump should exceed threshold"


def test_no_cliff_when_gradual():
    """Test that gradual volume increases don't trigger false positives."""
    market = Market(question="Will this happen?", tokens=[])
    token = Token(outcome="Yes", price=0.90, token_id="test123")

    # Order book with gradual increases (no cliff)
    order_book = OrderBook(
        name="Test",
        bids=[],
        asks=[
            OrderBookLevel(price=0.90, size=100.0 / 0.90),  # ~$111
            OrderBookLevel(price=0.91, size=120.0 / 0.91),  # ~$131
            OrderBookLevel(price=0.92, size=140.0 / 0.92),  # ~$152
        ],
    )

    opp = analyze_order_book(
        market,
        token,
        order_book,
        min_volume_jump=1000.0,
        min_price_gap_cents=1.0,
    )

    assert opp is None, "Should not detect cliff in gradual volume increase"


def test_order_book_spread():
    """Test that order book correctly identifies best bid and ask."""
    order_book = OrderBook(
        name="Test Market",
        bids=[
            OrderBookLevel(price=0.48, size=1000.0),
            OrderBookLevel(price=0.47, size=500.0),
        ],
        asks=[
            OrderBookLevel(price=0.52, size=800.0),
            OrderBookLevel(price=0.53, size=300.0),
        ],
    )

    # Best bid should be highest price
    assert order_book.bids[0].price == 0.48
    # Best ask should be lowest price
    assert order_book.asks[0].price == 0.52
    # Spread is the difference
    spread = order_book.asks[0].price - order_book.bids[0].price
    assert abs(spread - 0.04) < 0.001  # Use tolerance for floating point comparison
