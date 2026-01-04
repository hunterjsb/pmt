"""Trading page for market order book and order placement."""

import json

import streamlit as st

from polymarket import (
    AuthenticatedClob,
    Gamma,
    create_authenticated_clob,
    get_order_book_depth,
)


def get_client() -> AuthenticatedClob | None:
    """Get or create authenticated client from session state."""
    if "client" not in st.session_state:
        use_proxy = st.session_state.get("use_proxy", False)
        st.write(f"DEBUG: Creating client with proxy={use_proxy}")
        st.session_state["client"] = create_authenticated_clob(proxy=use_proxy)
    return st.session_state["client"]


def get_gamma() -> Gamma:
    """Get or create Gamma client from session state."""
    if "gamma" not in st.session_state:
        st.session_state["gamma"] = Gamma(
            proxy=st.session_state.get("use_proxy", False)
        )
    return st.session_state["gamma"]


def parse_market_query(query: str) -> dict | None:
    """Parse a market query (URL, slug, or search) and return market data."""
    gamma = get_gamma()

    if "polymarket.com" in query:
        from urllib.parse import urlparse

        parsed = urlparse(query)
        path = parsed.path.rstrip("/")
        parts = path.split("/")

        if "/event/" in path:
            import requests

            # URL format: /event/{event_slug} or /event/{event_slug}/{market_slug}
            event_slug = parts[2] if len(parts) > 2 else None
            market_slug = parts[3] if len(parts) > 3 else None

            if not event_slug:
                return None

            r = requests.get(
                f"https://gamma-api.polymarket.com/events/slug/{event_slug}"
            )
            if r.ok:
                event_data = r.json()
                # If market_slug provided, find and select that specific market
                if market_slug:
                    for m in event_data.get("markets", []):
                        if m.get("slug") == market_slug:
                            return {"type": "market", "data": m}
                return {"type": "event", "data": event_data}
        else:
            slug = parts[-1] if parts else query
            try:
                return {"type": "market", "data": gamma.market_by_slug(slug)}
            except Exception:
                pass
    else:
        # Try as slug first
        try:
            return {"type": "market", "data": gamma.market_by_slug(query)}
        except Exception:
            # Fall back to search
            results = gamma.search(query, limit=10)
            if results:
                return {"type": "search", "data": results}

    return None


def render_market_search():
    """Render market search in main pane."""
    with st.form("market_search_form"):
        query = st.text_input(
            "Find Market",
            placeholder="Paste URL, slug, or search term",
            key="market_query",
        )
        submitted = st.form_submit_button("🔍 Search", use_container_width=True)

    if not submitted or not query:
        # Show cached results if available
        if "search_result" in st.session_state and st.session_state["search_result"]:
            render_search_results(st.session_state["search_result"])
        return

    with st.spinner("Searching..."):
        result = parse_market_query(query)
        st.session_state["search_result"] = result

    if result:
        render_search_results(result)
    else:
        st.error("Market not found")


def render_search_results(result: dict):
    """Render search results for selection."""
    if result["type"] == "event":
        event = result["data"]
        markets = event.get("markets", [])
        st.write(f"**{event.get('title', 'Event')}**")

        cols = st.columns(min(len(markets), 3))
        for i, m in enumerate(markets):
            question = m.get("question", f"Market {i}")[:60]
            with cols[i % 3]:
                if st.button(
                    question, key=f"event_market_{i}", use_container_width=True
                ):
                    st.session_state["selected_market"] = m
                    st.session_state["search_result"] = None
                    st.rerun()

    elif result["type"] == "market":
        st.session_state["selected_market"] = result["data"]
        st.session_state["search_result"] = None
        st.rerun()

    elif result["type"] == "search":
        st.write("**Search Results:**")
        for i, m in enumerate(result["data"][:10]):
            question = m.get("question", f"Market {i}")[:60]
            if st.button(question, key=f"search_{i}", use_container_width=True):
                st.session_state["selected_market"] = m
                st.session_state["search_result"] = None
                st.rerun()


def render_order_book(token_id: str, outcome: str):
    """Render order book visualization."""
    st.write(f"**📖 Order Book: {outcome}**")

    try:
        book = get_order_book_depth(token_id)
    except Exception as e:
        st.error(f"Failed to load order book: {e}")
        return

    col_bids, col_asks = st.columns(2)

    with col_bids:
        st.write("**Bids (Buy)**")
        if book.bids:
            for level in book.bids[:10]:
                st.markdown(
                    f"<span style='color: green'>{level.price:.1%}</span> × {level.size:,.0f}",
                    unsafe_allow_html=True,
                )
        else:
            st.write("No bids")

    with col_asks:
        st.write("**Asks (Sell)**")
        if book.asks:
            for level in book.asks[:10]:
                st.markdown(
                    f"<span style='color: red'>{level.price:.1%}</span> × {level.size:,.0f}",
                    unsafe_allow_html=True,
                )
        else:
            st.write("No asks")

    if book.bids and book.asks:
        spread = book.asks[0].price - book.bids[0].price
        mid = (book.asks[0].price + book.bids[0].price) / 2
        st.caption(f"Mid: {mid:.1%} | Spread: {spread:.1%}")


def render_order_form(token_id: str, outcome: str):
    """Render order entry form."""
    client = get_client()

    st.write("**📝 Place Order**")

    if not client:
        st.warning("Connect wallet to trade")
        return

    order_type = st.radio(
        "Type", ["Limit", "Market"], horizontal=True, key="order_type"
    )
    side = st.radio("Side", ["BUY", "SELL"], horizontal=True, key="order_side")

    if order_type == "Limit":
        price = st.number_input(
            "Price",
            min_value=0.01,
            max_value=0.99,
            value=0.50,
            step=0.01,
            format="%.2f",
            key="limit_price",
        )
        size = st.number_input(
            "Shares",
            min_value=1.0,
            value=10.0,
            step=1.0,
            key="limit_size",
        )

        cost = price * size
        st.caption(f"Total: ${cost:.2f}")

        if st.button("Place Limit Order", type="primary", use_container_width=True):
            with st.spinner("Placing order..."):
                try:
                    result = client.post_order(
                        token_id=token_id,
                        price=price,
                        size=size,
                        side=side,
                    )
                    st.success(f"Order placed! ID: {result.get('orderID', 'N/A')}")
                except Exception as e:
                    st.error(f"Order failed: {e}")

    else:  # Market order
        amount = st.number_input(
            "Amount ($)" if side == "BUY" else "Shares",
            min_value=1.0,
            value=10.0,
            step=1.0,
            key="market_amount",
        )

        st.warning("⚠️ Market orders execute immediately at best price")

        if st.button("Place Market Order", type="primary", use_container_width=True):
            with st.spinner("Executing market order..."):
                try:
                    result = client.market_order(
                        token_id=token_id,
                        amount=amount,
                        side=side,
                    )
                    st.success(f"Order executed! ID: {result.get('orderID', 'N/A')}")
                except Exception as e:
                    st.error(f"Order failed: {e}")


def render_market_positions(token_id: str):
    """Render positions and orders for this specific market."""
    client = get_client()
    if not client:
        return

    st.write("**📊 Your Position**")

    try:
        # Get token balance
        balance = client.token_balance(token_id)
        if balance > 0.01:
            st.metric("Shares Held", f"{balance:,.2f}")
        else:
            st.caption("No position in this market")
    except Exception as e:
        st.caption(f"Could not load position: {e}")

    # Show open orders for this market
    try:
        orders = client.open_orders(asset_id=token_id)
        if orders:
            st.write("**Open Orders:**")
            for order in orders:
                col1, col2, col3 = st.columns([2, 1, 1])
                col1.write(f"{order.get('side')} @ {float(order.get('price', 0)):.1%}")
                col2.write(f"{float(order.get('size', 0)):,.0f}")
                if col3.button("❌", key=f"cancel_{order.get('id')}"):
                    try:
                        client.cancel(order.get("id"))
                        st.rerun()
                    except Exception as e:
                        st.error(f"Cancel failed: {e}")
    except Exception:
        pass


def render_trading_page():
    """Main trading page entry point."""
    st.title("📈 Trading")

    # Check if we have a selected market
    market = st.session_state.get("selected_market")

    if not market:
        render_market_search()
        return

    # Show current market with option to change
    col1, col2 = st.columns([4, 1])
    with col2:
        if st.button("🔄 Change Market"):
            st.session_state.pop("selected_market", None)
            st.rerun()

    # Market header
    st.subheader(market.get("question", "Unknown Market"))

    # Market info row
    col1, col2, col3, col4 = st.columns(4)
    liquidity = market.get("liquidity", 0)
    volume = market.get("volume", 0)
    col1.metric("Liquidity", f"${float(liquidity or 0):,.0f}")
    col2.metric("Volume", f"${float(volume or 0):,.0f}")
    col3.metric("Status", "Open" if market.get("active") else "Closed")

    # Parse tokens, outcomes, and prices
    tokens = market.get("clobTokenIds") or market.get("tokens", [])
    outcomes = market.get("outcomes", ["Yes", "No"])
    outcome_prices = market.get("outcomePrices", [])

    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except json.JSONDecodeError:
            tokens = []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = ["Yes", "No"]
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except json.JSONDecodeError:
            outcome_prices = []

    if not tokens:
        st.warning("No tradeable tokens found for this market")
        return

    # Outcome selector
    with col4:
        if len(tokens) > 1 and len(outcomes) == len(tokens):
            selected_idx = st.selectbox(
                "Outcome",
                range(len(outcomes)),
                format_func=lambda i: outcomes[i],
                key="outcome_select",
            )
        else:
            selected_idx = 0

    token_id = (
        tokens[selected_idx]
        if isinstance(tokens[0], str)
        else tokens[selected_idx].get("token_id")
    )
    outcome_name = outcomes[selected_idx] if selected_idx < len(outcomes) else "Yes"

    # Display implied probabilities for all outcomes
    st.markdown("---")
    st.write("**Implied Probabilities**")
    prob_cols = st.columns(len(outcomes))
    for i, (outcome, col) in enumerate(zip(outcomes, prob_cols)):
        if i < len(outcome_prices):
            prob = float(outcome_prices[i]) * 100
            col.metric(outcome, f"{prob:.1f}%")
        else:
            col.metric(outcome, "N/A")

    st.markdown("---")

    # Three column layout: order book, order form, positions
    col_book, col_form, col_pos = st.columns([2, 1, 1])

    with col_book:
        render_order_book(token_id, outcome_name)
        if st.button("🔄 Refresh Book"):
            st.rerun()

    with col_form:
        render_order_form(token_id, outcome_name)

    with col_pos:
        render_market_positions(token_id)
