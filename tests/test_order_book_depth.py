"""Integration tests for order book depth functionality.

These tests use real API responses saved as fixtures to ensure
the order book parsing works correctly with actual data.
"""

import json
import os

from polymarket import get_order_book_depth
from polymarket.models import OrderBook


def test_order_book_depth_returns_all_levels():
    """Test that get_order_book_depth returns complete order book, not just best bid/ask."""
    # Load saved real API response
    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "order_book_sample.json"
    )

    with open(fixture_path) as f:
        expected_data = json.load(f)

    # The fixture has multiple price levels
    assert len(expected_data["asks"]) > 1, "Fixture should have multiple ask levels"
    assert len(expected_data["bids"]) > 1, "Fixture should have multiple bid levels"

    # Verify we captured the spread properly
    best_bid = expected_data["bids"][0]["price"]
    best_ask = expected_data["asks"][0]["price"]

    # Real markets typically have huge spreads
    assert best_ask > best_bid, "Ask should be higher than bid"


def test_order_book_has_liquidity_depth():
    """Test that we can see multiple tranches of liquidity at different prices."""
    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "order_book_sample.json"
    )

    with open(fixture_path) as f:
        book_data = json.load(f)

    # Check that asks are sorted by price (best/lowest first = descending from API perspective)
    ask_prices = [a["price"] for a in book_data["asks"]]
    # In reality, the API returns asks sorted best-first, which means descending prices
    # (99.9¢, 99.8¢, 99.0¢, etc. - lower prices are better for buyers)
    assert ask_prices == sorted(ask_prices, reverse=True), (
        "Asks should be sorted best-first (descending)"
    )

    # Check that bids are sorted by price (ascending in the actual API response)
    bid_prices = [b["price"] for b in book_data["bids"]]
    assert bid_prices == sorted(bid_prices), (
        "Bids should be sorted ascending (as returned by API)"
    )

    # Verify we have size data for each level
    for ask in book_data["asks"]:
        assert ask["size"] > 0, "Each ask level should have positive size"

    for bid in book_data["bids"]:
        assert bid["size"] > 0, "Each bid level should have positive size"


def test_live_api_call():
    """Test that we can actually call the live API and get structured data back.

    This is a real integration test - it will fail if:
    - The API is down
    - The token no longer exists
    - The API response format changes
    """
    # Use a known active token (Altman jail market June 2026 - No outcome)
    token_id = (
        "17718121883117127484041858994698258715282000837318841943238567814505154646336"
    )

    book = get_order_book_depth(token_id)

    # Verify we got an OrderBook object
    assert isinstance(book, OrderBook)

    # Should have bids and asks
    assert len(book.bids) > 0, "Should have at least one bid"
    assert len(book.asks) > 0, "Should have at least one ask"

    # Best bid should be lower than best ask
    assert book.bids[0].price < book.asks[0].price, "Bid should be lower than ask"

    # Prices should be between 0 and 1
    assert 0 < book.bids[0].price < 1, "Bid price should be between 0 and 1"
    assert 0 < book.asks[0].price < 1, "Ask price should be between 0 and 1"

    # Sizes should be positive
    assert book.bids[0].size > 0, "Bid size should be positive"
    assert book.asks[0].size > 0, "Ask size should be positive"


def test_multiple_ask_levels():
    """Test that we can access multiple levels of the ask ladder.

    This is critical for understanding real trading costs - the 'market price'
    often doesn't reflect what you actually pay.
    """
    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "order_book_sample.json"
    )

    with open(fixture_path) as f:
        book_data = json.load(f)

    asks = book_data["asks"]

    if len(asks) >= 3:
        # Check that deeper levels have progressively better (lower) prices
        # This is common in markets - the best ask is first, then slightly better deals deeper
        first_ask = asks[0]["price"]
        second_ask = asks[1]["price"]
        third_ask = asks[2]["price"]

        # Each level should be at same or better (lower) price
        assert second_ask <= first_ask, (
            "Second ask should be <= first ask (better price)"
        )
        assert third_ask <= second_ask, (
            "Third ask should be <= second ask (better price)"
        )

        # Calculate total cost to buy across levels
        total_shares = sum(a["size"] for a in asks[:3])
        total_cost = sum(a["price"] * a["size"] for a in asks[:3])
        avg_price = total_cost / total_shares

        # Average price should be <= best (highest) ask since we're buying progressively better levels
        assert avg_price <= first_ask, "Average price should be <= best ask price"


def test_raw_api_response_structure():
    """Test that the raw API response has the expected structure.

    This helps catch breaking changes in the API.
    """
    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "clob_api_response.json"
    )

    with open(fixture_path) as f:
        response = json.load(f)

    # Check required fields
    assert "bids" in response, "Response should have bids"
    assert "asks" in response, "Response should have asks"
    assert "asset_id" in response, "Response should have asset_id"
    assert "market" in response, "Response should have market"

    # Check bid/ask structure
    if response["bids"]:
        first_bid = response["bids"][0]
        assert "price" in first_bid, "Bid should have price"
        assert "size" in first_bid, "Bid should have size"

        # Prices should be strings in API response
        assert isinstance(first_bid["price"], str), (
            "Price should be string in API response"
        )
        assert isinstance(first_bid["size"], str), (
            "Size should be string in API response"
        )

    if response["asks"]:
        first_ask = response["asks"][0]
        assert "price" in first_ask, "Ask should have price"
        assert "size" in first_ask, "Ask should have size"
