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
uv run pytest tests/test_models.py::test_token_creation
```

## Test Structure

- `test_models.py` - Tests for domain models (Token, Market, OrderBook, Event)
- `test_formatting.py` - Tests for console formatting utilities

## Writing New Tests

Tests follow these conventions:
- Test files must start with `test_`
- Test functions must start with `test_`
- Use descriptive test names that explain what is being tested
- Keep tests simple and focused on a single behavior

Example:
```python
def test_token_creation():
    """Test basic Token creation."""
    token = Token(outcome="Yes", price=0.65, token_id="abc123")
    assert token.outcome == "Yes"
    assert token.price == 0.65
```

## CI/CD

Tests are automatically run on GitHub Actions for:
- Every push to `main`/`master` branches
- Every pull request targeting `main`/`master` branches

See `.github/workflows/ci.yml` for the full CI configuration.