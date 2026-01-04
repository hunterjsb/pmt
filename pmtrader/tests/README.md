# Tests

This directory contains unit tests for the pmt (Polymarket Trader) project.

## Running Tests

### Run all tests
```bash
uv run pytest
```

### Run tests with verbose output
```bash
uv run pytest -v
```

### Run a specific test file
```bash
uv run pytest tests/test_models.py
```

### Run a specific test function
```bash
uv run pytest tests/test_models.py::test_volume_cliff_detection
```

## Test Coverage

Currently testing the core business logic:

- **`test_models.py`** - Tests for order book analysis and scanner logic
  - `test_volume_cliff_detection()` - Verifies we correctly identify volume cliffs (thin levels followed by thick levels)
  - `test_no_cliff_when_gradual()` - Ensures gradual volume increases don't trigger false positives
  - `test_order_book_spread()` - Validates order book bid/ask spread calculations

## Writing New Tests

Tests follow these conventions:
- Test files must start with `test_`
- Test functions must start with `test_`
- Use descriptive test names that explain what behavior is being tested
- Focus on testing business logic, not language features
- Keep tests simple and focused on a single behavior

Example:
```python
def test_volume_cliff_detection():
    """Test that we detect a volume cliff when dollar value jumps significantly."""
    market = Market(question="Will this happen?", tokens=[])
    token = Token(outcome="Yes", price=0.93, token_id="test123")
    
    order_book = OrderBook(
        name="Test",
        bids=[],
        asks=[
            OrderBookLevel(price=0.93, size=100.0 / 0.93),
            OrderBookLevel(price=0.96, size=3000.0 / 0.96),  # CLIFF!
        ],
    )
    
    opp = analyze_order_book(market, token, order_book, min_volume_jump=1000.0)
    assert opp is not None, "Should detect volume cliff opportunity"
```

## CI/CD

Tests are automatically run on GitHub Actions for:
- Every push to `main`/`master` branches
- Every pull request targeting `main`/`master` branches

See `.github/workflows/ci.yml` for the full CI configuration.