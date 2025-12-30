"""Basic unit tests for Polymarket domain models."""

from polymarket.models import Event, Market, OrderBook, OrderBookLevel, Token


def test_token_creation():
    """Test basic Token creation."""
    token = Token(outcome="Yes", price=0.65, token_id="abc123")
    assert token.outcome == "Yes"
    assert token.price == 0.65
    assert token.token_id == "abc123"


def test_token_string_representation():
    """Test Token string formatting."""
    token = Token(outcome="Yes", price=0.65, token_id="abc123")
    result = str(token)
    assert "Yes" in result
    assert "65.0%" in result


def test_token_with_no_price():
    """Test Token with None price."""
    token = Token(outcome="No", price=None, token_id="xyz789")
    result = str(token)
    assert "N/A" in result


def test_market_creation():
    """Test basic Market creation."""
    tokens = [
        Token(outcome="Yes", price=0.55, token_id="token1"),
        Token(outcome="No", price=0.45, token_id="token2"),
    ]
    market = Market(question="Will this test pass?", tokens=tokens)
    assert market.question == "Will this test pass?"
    assert len(market.tokens) == 2


def test_market_string_representation():
    """Test Market string formatting."""
    tokens = [Token(outcome="Yes", price=0.75, token_id="id1")]
    market = Market(question="Test question?", tokens=tokens)
    result = str(market)
    assert "Test question?" in result


def test_order_book_level_creation():
    """Test OrderBookLevel creation."""
    level = OrderBookLevel(price=0.52, size=1000.0)
    assert level.price == 0.52
    assert level.size == 1000.0


def test_order_book_level_string():
    """Test OrderBookLevel string formatting."""
    level = OrderBookLevel(price=0.52, size=1000.0)
    result = str(level)
    assert "52.0%" in result
    assert "1,000" in result


def test_order_book_creation():
    """Test OrderBook creation."""
    bids = [OrderBookLevel(price=0.50, size=500.0)]
    asks = [OrderBookLevel(price=0.52, size=600.0)]
    book = OrderBook(name="Test Market", bids=bids, asks=asks)
    assert book.name == "Test Market"
    assert len(book.bids) == 1
    assert len(book.asks) == 1


def test_order_book_string_representation():
    """Test OrderBook string formatting."""
    bids = [OrderBookLevel(price=0.48, size=1000.0)]
    asks = [OrderBookLevel(price=0.52, size=1500.0)]
    book = OrderBook(name="Order Book Test", bids=bids, asks=asks)
    result = str(book)
    assert "Order Book Test" in result
    assert "1 bids" in result
    assert "1 asks" in result


def test_event_creation():
    """Test Event creation."""
    event = Event(
        title="Test Event",
        slug="test-event",
        end_date="2024-12-31",
        liquidity=50000.0,
        volume=100000.0,
    )
    assert event.title == "Test Event"
    assert event.slug == "test-event"
    assert event.end_date == "2024-12-31"
    assert event.liquidity == 50000.0
    assert event.volume == 100000.0


def test_event_with_none_values():
    """Test Event with optional None values."""
    event = Event(
        title="Minimal Event",
        slug="minimal",
        end_date=None,
        liquidity=None,
        volume=None,
    )
    assert event.title == "Minimal Event"
    assert event.end_date is None
    assert event.liquidity is None


def test_event_string_representation():
    """Test Event string formatting."""
    event = Event(
        title="String Test Event",
        slug="string-test",
        end_date="2024-12-31",
        liquidity=10000.0,
        volume=20000.0,
    )
    result = str(event)
    assert "String Test Event" in result
    assert "string-test" in result
