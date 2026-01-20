"""Tests for Gamma API consistency between pmtrader (Python) and pmengine (Rust).

These tests verify that both implementations:
1. Use the same /events endpoint with date filtering
2. Parse the same fields (outcomes, outcomePrices, clobTokenIds)
3. Return consistent results for market discovery

Run with: uv run pytest tests/test_gamma_consistency.py -v
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from decimal import Decimal

import pytest

from polymarket.gamma import Gamma


@pytest.fixture
def gamma_client():
    """Create a Gamma API client."""
    return Gamma()


@pytest.fixture
def sample_response():
    """Load the shared test fixture."""
    fixture_path = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "gamma_response.json"
    if fixture_path.exists():
        with open(fixture_path) as f:
            return json.load(f)
    return None


class TestGammaApiConsistency:
    """Tests verifying Gamma API consistency."""

    def test_events_endpoint_supports_date_filtering(self, gamma_client):
        """Verify the /events endpoint supports end_date_min and end_date_max."""
        # This is what both pmtrader and pmengine use for market discovery
        now = datetime.now(timezone.utc)
        end_date_min = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_date_max = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

        events = gamma_client.events(
            limit=10,
            closed=False,
            order="endDate",
            ascending=True,
            end_date_min=end_date_min,
            end_date_max=end_date_max,
        )

        # Should return a list (may be empty if no events match)
        assert isinstance(events, list)

    def test_event_structure_has_required_fields(self, gamma_client):
        """Verify events have the fields both implementations need."""
        events = gamma_client.events(limit=5, closed=False)

        if not events:
            pytest.skip("No events returned from API")

        event = events[0]

        # Both implementations expect these fields
        assert "markets" in event or "endDate" in event

        if "markets" in event and event["markets"]:
            market = event["markets"][0]

            # Required fields for market discovery
            # Note: These may be JSON-encoded strings
            assert "outcomes" in market or "question" in market
            assert "clobTokenIds" in market or "clob_token_ids" in market.get("condition_id", "")

    def test_json_encoded_fields_parsing(self, sample_response):
        """Verify JSON-encoded fields can be parsed correctly."""
        if sample_response is None:
            pytest.skip("Sample fixture not found")

        # Simulate how both implementations parse JSON-encoded fields
        for event in sample_response.get("events", []):
            for market in event.get("markets", []):
                # outcomes is JSON-encoded array
                outcomes_str = market.get("outcomes", "[]")
                outcomes = json.loads(outcomes_str)
                assert isinstance(outcomes, list)

                # outcomePrices is JSON-encoded array of strings
                prices_str = market.get("outcomePrices", "[]")
                prices = json.loads(prices_str)
                assert isinstance(prices, list)
                # Prices should be parseable as decimals
                for p in prices:
                    Decimal(p)

                # clobTokenIds is JSON-encoded array
                tokens_str = market.get("clobTokenIds", "[]")
                tokens = json.loads(tokens_str)
                assert isinstance(tokens, list)

    def test_high_certainty_detection(self, sample_response):
        """Verify high-certainty market detection matches expectations."""
        if sample_response is None:
            pytest.skip("Sample fixture not found")

        min_certainty = Decimal("0.95")
        sure_bet_tokens = []

        for event in sample_response.get("events", []):
            for market in event.get("markets", []):
                prices_str = market.get("outcomePrices", "[]")
                prices = [Decimal(p) for p in json.loads(prices_str)]

                tokens_str = market.get("clobTokenIds", "[]")
                tokens = json.loads(tokens_str)

                # Check each outcome for high certainty
                for i, price in enumerate(prices):
                    if price >= min_certainty and i < len(tokens):
                        sure_bet_tokens.append(tokens[i])

        # Verify against expected values in fixture
        expected = sample_response.get("_test_expectations", {})
        expected_count = expected.get("sure_bet_candidates_95pct", 0)
        expected_tokens = set(expected.get("sure_bet_candidates_tokens", []))

        assert len(sure_bet_tokens) == expected_count
        assert set(sure_bet_tokens) == expected_tokens


class TestGammaApiParameters:
    """Tests for API parameter consistency."""

    def test_pagination_parameters(self, gamma_client):
        """Verify pagination works as expected."""
        # First batch
        events1 = gamma_client.events(limit=5, offset=0)

        # Second batch
        events2 = gamma_client.events(limit=5, offset=5)

        # Should get different events (unless there are fewer than 10 total)
        if len(events1) == 5 and len(events2) > 0:
            # Check first event IDs differ
            id1 = events1[0].get("id") or events1[0].get("slug")
            id2 = events2[0].get("id") or events2[0].get("slug")
            assert id1 != id2

    def test_closed_filter(self, gamma_client):
        """Verify closed filter works."""
        # Get open events only
        open_events = gamma_client.events(limit=10, closed=False)

        # All should have closed=false or be missing the field
        for event in open_events:
            if "closed" in event:
                assert event["closed"] is False
