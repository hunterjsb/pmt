"""Broker page for Polymarket trading UI."""

import streamlit as st

from polymarket import (
    AuthenticatedClob,
    Clob,
    Gamma,
    create_authenticated_clob,
    get_order_book_depth,
)


def get_client() -> AuthenticatedClob | None:
    """Get or create authenticated client from session state."""
    if "client" not in st.session_state:
        st.session_state["client"] = create_authenticated_clob(
            proxy=st.session_state.get("use_proxy", False)
        )
    return st.session_state["client"]


def get_gamma() -> Gamma:
    """Get or create Gamma client from session state."""
    if "gamma" not in st.session_state:
        st.session_state["gamma"] = Gamma(
            proxy=st.session_state.get("use_proxy", False)
        )
    return st.session_state["gamma"]


def get_clob() -> Clob:
    """Get or create read-only CLOB client from session state."""
    if "clob" not in st.session_state:
        st.session_state["clob"] = Clob(proxy=st.session_state.get("use_proxy", False))
    return st.session_state["clob"]


def render_account_overview():
    """Render the account overview section with balance, holdings, orders, history."""
    client = get_client()

    if not client:
        st.warning(
            "⚠️ Not authenticated. Set PM_PRIVATE_KEY and PM_FUNDER_ADDRESS in .env"
        )
        return

    # Account header with balance
    col1, col2, col3 = st.columns([2, 1, 1])

    with col1:
        st.subheader("Account Overview")

    with col2:
        if st.button("🔄 Refresh", key="refresh_account"):
            # Clear cached data
            for key in ["usdc_balance", "positions", "open_orders", "trades"]:
                st.session_state.pop(key, None)
            st.rerun()

    # Fetch data with caching
    with st.spinner("Loading account data..."):
        try:
            if "usdc_balance" not in st.session_state:
                st.session_state["usdc_balance"] = client.usdc_balance()
            usdc = st.session_state["usdc_balance"]
        except Exception as e:
            st.error(f"Failed to fetch balance: {e}")
            usdc = 0.0

    # Balance display
    st.metric("💵 USDC Balance", f"${usdc:,.2f}")

    # Tabs for holdings, orders, history
    tab_holdings, tab_orders, tab_history = st.tabs(
        ["📊 Holdings", "📋 Open Orders", "📜 History"]
    )

    with tab_holdings:
        render_holdings(client)

    with tab_orders:
        render_open_orders(client)

    with tab_history:
        render_trade_history(client)


def render_holdings(client: AuthenticatedClob):
    """Render current positions/holdings."""
    try:
        if "positions" not in st.session_state:
            st.session_state["positions"] = client.positions()
        positions = st.session_state["positions"]
    except Exception as e:
        st.error(f"Failed to fetch positions: {e}")
        return

    if not positions:
        st.info("No open positions")
        return

    # Display as a table
    st.dataframe(
        positions,
        column_config={
            "token_id": st.column_config.TextColumn("Token ID", width="small"),
            "market": st.column_config.TextColumn("Market", width="large"),
            "outcome": st.column_config.TextColumn("Outcome", width="small"),
            "shares": st.column_config.NumberColumn(
                "Shares", format="%.2f", width="small"
            ),
        },
        hide_index=True,
        use_container_width=True,
    )


def render_open_orders(client: AuthenticatedClob):
    """Render open orders with cancel functionality."""
    try:
        if "open_orders" not in st.session_state:
            st.session_state["open_orders"] = client.open_orders()
        orders = st.session_state["open_orders"]
    except Exception as e:
        st.error(f"Failed to fetch orders: {e}")
        return

    if not orders:
        st.info("No open orders")
        return

    # Cancel all button
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("❌ Cancel All", type="secondary"):
            try:
                client.cancel_all()
                st.session_state.pop("open_orders", None)
                st.success("All orders cancelled")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to cancel orders: {e}")

    # Display orders with individual cancel buttons
    for i, order in enumerate(orders):
        with st.container():
            cols = st.columns([3, 1, 1, 1, 1])
            cols[0].write(order.get("market", "Unknown")[:40])
            cols[1].write(order.get("side", "?"))
            cols[2].write(f"{float(order.get('price', 0)):.1%}")
            cols[3].write(f"{float(order.get('size', 0)):,.0f}")
            if cols[4].button("❌", key=f"cancel_{i}"):
                try:
                    client.cancel(order.get("id"))
                    st.session_state.pop("open_orders", None)
                    st.rerun()
                except Exception as e:
                    st.error(f"Cancel failed: {e}")


def render_trade_history(client: AuthenticatedClob):
    """Render trade history."""
    try:
        if "trades" not in st.session_state:
            st.session_state["trades"] = client.trades()
        trades = st.session_state["trades"]
    except Exception as e:
        st.error(f"Failed to fetch trades: {e}")
        return

    if not trades:
        st.info("No trade history")
        return

    # Display recent trades
    st.dataframe(
        trades[:50],  # Show last 50
        hide_index=True,
        use_container_width=True,
    )


def render_market_lookup():
    """Render market search/lookup section."""
    st.subheader("🔍 Market Lookup")

    query = st.text_input(
        "Enter market ID, slug, URL, or search term",
        placeholder="e.g., will-trump-win-2024 or paste a Polymarket URL",
        key="market_query",
    )

    if not query:
        return

    gamma = get_gamma()
    market_data = None

    # Try to parse the query
    with st.spinner("Looking up market..."):
        try:
            # Check if it's a URL
            if "polymarket.com" in query:
                # Extract slug from URL
                # URLs look like: https://polymarket.com/event/will-trump-win
                parts = query.rstrip("/").split("/")
                slug = parts[-1] if parts else query
                market_data = gamma.market_by_slug(slug)
            else:
                # Try as slug first
                try:
                    market_data = gamma.market_by_slug(query)
                except Exception:
                    # Fall back to search
                    results = gamma.search(query, limit=5)
                    if results:
                        # Let user pick from results
                        st.write("**Search Results:**")
                        for i, result in enumerate(results):
                            question = result.get("question", "Unknown")
                            if st.button(question[:60], key=f"search_{i}"):
                                market_data = result
                                break
        except Exception as e:
            st.error(f"Lookup failed: {e}")
            return

    if market_data:
        st.session_state["selected_market"] = market_data
        render_market_trading(market_data)


def render_market_trading(market: dict):
    """Render trading interface for a selected market."""
    st.markdown("---")
    st.subheader(market.get("question", "Unknown Market"))

    # Market info
    col1, col2, col3 = st.columns(3)
    col1.metric("Liquidity", f"${market.get('liquidity', 0):,.0f}")
    col2.metric("Volume", f"${market.get('volume', 0):,.0f}")
    col3.metric("Status", "Open" if market.get("active") else "Closed")

    # Get tokens/outcomes
    tokens = market.get("clobTokenIds") or market.get("tokens", [])
    outcomes = market.get("outcomes", ["Yes", "No"])

    if not tokens:
        st.warning("No tradeable tokens found for this market")
        return

    # Token selector (for markets with multiple outcomes)
    if len(tokens) > 1 and len(outcomes) == len(tokens):
        selected_idx = st.selectbox(
            "Select Outcome",
            range(len(outcomes)),
            format_func=lambda i: outcomes[i],
        )
        token_id = (
            tokens[selected_idx]
            if isinstance(tokens[0], str)
            else tokens[selected_idx].get("token_id")
        )
        outcome_name = outcomes[selected_idx]
    else:
        token_id = (
            tokens[0] if isinstance(tokens[0], str) else tokens[0].get("token_id")
        )
        outcome_name = outcomes[0] if outcomes else "Yes"

    st.session_state["selected_token_id"] = token_id
    st.session_state["selected_outcome"] = outcome_name

    # Order book and trading side by side
    col_book, col_trade = st.columns([2, 1])

    with col_book:
        render_order_book(token_id, outcome_name)

    with col_trade:
        render_order_form(token_id, outcome_name)


def render_order_book(token_id: str, outcome: str):
    """Render order book visualization."""

    st.write(f"**📖 Order Book: {outcome}**")

    try:
        book = get_order_book_depth(token_id)
    except Exception as e:
        st.error(f"Failed to load order book: {e}")
        return

    # Display as two columns: bids (green) and asks (red)
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

    # Spread info
    if book.bids and book.asks:
        spread = book.asks[0].price - book.bids[0].price
        st.caption(f"Spread: {spread:.1%}")


def render_order_form(token_id: str, outcome: str):
    """Render order entry form."""
    client = get_client()

    st.write("**📝 Place Order**")

    if not client:
        st.warning("Connect wallet to trade")
        return

    # Order type
    order_type = st.radio("Type", ["Limit", "Market"], horizontal=True)

    # Side
    side = st.radio("Side", ["BUY", "SELL"], horizontal=True)

    if order_type == "Limit":
        price = st.number_input(
            "Price",
            min_value=0.01,
            max_value=0.99,
            value=0.50,
            step=0.01,
            format="%.2f",
        )
        size = st.number_input(
            "Shares",
            min_value=1.0,
            value=10.0,
            step=1.0,
        )

        # Cost preview
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
                    # Clear cached orders
                    st.session_state.pop("open_orders", None)
                except Exception as e:
                    st.error(f"Order failed: {e}")

    else:  # Market order
        amount = st.number_input(
            "Amount ($)" if side == "BUY" else "Shares",
            min_value=1.0,
            value=10.0,
            step=1.0,
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
                    # Clear cached data
                    st.session_state.pop("open_orders", None)
                    st.session_state.pop("positions", None)
                    st.session_state.pop("usdc_balance", None)
                except Exception as e:
                    st.error(f"Order failed: {e}")


def render_broker_page():
    """Main broker page entry point."""
    st.title("🏦 Broker")

    # Two main sections: account overview and market trading
    render_account_overview()

    st.markdown("---")

    render_market_lookup()
