"""Broker page for account overview."""

from datetime import datetime

import streamlit as st

from polymarket import AuthenticatedClob, Clob, create_authenticated_clob


def get_client() -> AuthenticatedClob | None:
    """Get or create authenticated client from session state."""
    if "client" not in st.session_state:
        st.session_state["client"] = create_authenticated_clob(
            proxy=st.session_state.get("use_proxy", False)
        )
    return st.session_state["client"]


def get_clob() -> Clob:
    """Get or create CLOB client."""
    if "clob" not in st.session_state:
        st.session_state["clob"] = Clob(
            proxy=st.session_state.get("use_proxy", False)
        )
    return st.session_state["clob"]


def get_market_name(condition_id: str) -> str:
    """Get market question from condition_id, with caching."""
    cache_key = f"mkt_{condition_id}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    try:
        clob = get_clob()
        market = clob.market(condition_id)
        name = market.get("question", condition_id[:16] + "...")
        st.session_state[cache_key] = name
        return name
    except Exception:
        return condition_id[:16] + "..."


def render_holdings(client: AuthenticatedClob):
    """Render current positions/holdings."""
    if "positions" not in st.session_state:
        # Show what we're doing
        status = st.empty()
        status.info("Fetching trade history...")

        try:
            trades = client.trades()
            status.info(f"Found {len(trades)} trades, checking balances...")
        except Exception as e:
            status.error(f"Failed to fetch trades: {e}")
            return

        # Build token metadata from trades
        token_meta = {}
        for t in trades:
            token_id = t["asset_id"]
            if token_id not in token_meta:
                token_meta[token_id] = {
                    "outcome": t["outcome"],
                    "market": t["market"],
                }

        status.info(f"Checking {len(token_meta)} token balances...")

        # Fetch balances with progress
        import time
        positions = []
        progress = st.progress(0)

        for i, (token_id, meta) in enumerate(list(token_meta.items())[:20]):
            progress.progress((i + 1) / min(len(token_meta), 20))
            if i > 0:
                time.sleep(0.3)
            try:
                bal = client.token_balance(token_id)
                if bal > 0.01:
                    positions.append({
                        "token_id": token_id,
                        "outcome": meta["outcome"],
                        "market": meta["market"],
                        "shares": bal,
                    })
            except Exception:
                continue

        progress.empty()
        status.empty()
        st.session_state["positions"] = positions

    positions = st.session_state["positions"]

    if not positions:
        st.info("No open positions")
        return

    # Header
    cols = st.columns([2, 1, 1])
    cols[0].write("**Market**")
    cols[1].write("**Outcome**")
    cols[2].write("**Shares**")

    # Format for display
    for pos in positions:
        cols = st.columns([2, 1, 1])
        cols[0].write(get_market_name(pos["market"]))
        cols[1].write(f"**{pos['outcome']}**")
        cols[2].write(f"{pos['shares']:,.2f}")


def render_open_orders(client: AuthenticatedClob):
    """Render open orders with cancel functionality."""
    if "open_orders" not in st.session_state:
        with st.spinner("Loading orders..."):
            try:
                st.session_state["open_orders"] = client.open_orders()
            except Exception as e:
                st.error(f"Failed to fetch orders: {e}")
                return

    orders = st.session_state["open_orders"]

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

    # Header
    cols = st.columns([2, 1, 1, 1, 1])
    cols[0].write("**Market**")
    cols[1].write("**Side**")
    cols[2].write("**Price**")
    cols[3].write("**Size**")
    cols[4].write("")

    # Display orders
    for i, order in enumerate(orders):
        cols = st.columns([2, 1, 1, 1, 1])
        cols[0].write(get_market_name(order.get("market", "")))
        cols[1].write(order.get("side", "?"))
        cols[2].write(f"{float(order.get('price', 0)):.0%}")
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
    if "trades" not in st.session_state:
        with st.spinner("Loading trades..."):
            try:
                st.session_state["trades"] = client.trades()
            except Exception as e:
                st.error(f"Failed to fetch trades: {e}")
                return

    trades = st.session_state["trades"]

    if not trades:
        st.info("No trade history")
        return

    # Header
    cols = st.columns([2, 1, 1, 1, 1, 1])
    cols[0].write("**Market**")
    cols[1].write("**Side**")
    cols[2].write("**Outcome**")
    cols[3].write("**Size**")
    cols[4].write("**Price**")
    cols[5].write("**Time**")

    # Show last 50 trades
    for trade in trades[:50]:
        cols = st.columns([2, 1, 1, 1, 1, 1])

        cols[0].write(get_market_name(trade.get("market", "")))

        side = trade.get("side", "?")
        side_color = "green" if side == "BUY" else "red"
        cols[1].markdown(f":{side_color}[{side}]")

        cols[2].write(trade.get("outcome", "?"))
        cols[3].write(f"{float(trade.get('size', 0)):,.0f}")
        cols[4].write(f"{float(trade.get('price', 0)):.0%}")

        # Format timestamp
        try:
            ts = int(trade.get("match_time", 0))
            dt = datetime.fromtimestamp(ts)
            cols[5].write(dt.strftime("%m/%d %H:%M"))
        except Exception:
            cols[5].write("-")


def render_broker_page():
    """Main broker page entry point."""
    st.title("🏦 Broker")

    client = get_client()

    if not client:
        st.warning(
            "⚠️ Not authenticated. Set PM_PRIVATE_KEY and PM_FUNDER_ADDRESS in .env"
        )
        return

    # Refresh button
    col1, col2 = st.columns([3, 1])
    with col1:
        st.subheader("Account Overview")
    with col2:
        if st.button("🔄 Refresh"):
            for key in ["usdc_balance", "positions", "open_orders", "trades"]:
                st.session_state.pop(key, None)
            st.rerun()

    # Balance - always load this
    if "usdc_balance" not in st.session_state:
        try:
            st.session_state["usdc_balance"] = client.usdc_balance()
        except Exception as e:
            st.error(f"Failed to fetch balance: {e}")
            st.session_state["usdc_balance"] = 0.0

    usdc = st.session_state["usdc_balance"]
    st.metric("💵 USDC Balance", f"${usdc:,.2f}")

    # Tabs - data loads only when tab is rendered
    tab_holdings, tab_orders, tab_history = st.tabs(
        ["📊 Holdings", "📋 Open Orders", "📜 History"]
    )

    with tab_holdings:
        render_holdings(client)

    with tab_orders:
        render_open_orders(client)

    with tab_history:
        render_trade_history(client)
