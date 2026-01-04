"""Broker page for account overview."""

import streamlit as st

from polymarket import AuthenticatedClob, create_authenticated_clob


def get_client() -> AuthenticatedClob | None:
    """Get or create authenticated client from session state."""
    if "client" not in st.session_state:
        st.session_state["client"] = create_authenticated_clob(
            proxy=st.session_state.get("use_proxy", False)
        )
    return st.session_state["client"]


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

    st.dataframe(
        trades[:50],
        hide_index=True,
        use_container_width=True,
    )


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

    # Balance
    try:
        if "usdc_balance" not in st.session_state:
            st.session_state["usdc_balance"] = client.usdc_balance()
        usdc = st.session_state["usdc_balance"]
    except Exception as e:
        st.error(f"Failed to fetch balance: {e}")
        usdc = 0.0

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
