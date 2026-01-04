"""Main Streamlit application entry point."""

import os
import sys

import streamlit as st
from dotenv import load_dotenv
from streamlit.web import cli as stcli

load_dotenv()


def run_app():
    """Run the Polymarket trading UI."""
    st.set_page_config(
        page_title="Polymarket Trader",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Sidebar navigation
    st.sidebar.title("📈 Polymarket")
    st.sidebar.markdown("---")

    page = st.sidebar.radio(
        "Navigation",
        ["🏦 Broker", "📈 Trading", "🔍 Scanner", "🤖 Bot"],
        label_visibility="collapsed",
    )

    st.sidebar.markdown("---")

    # Connection settings in sidebar
    proxy_url = os.environ.get("PMPROXY_URL", "")
    with st.sidebar.expander("⚙️ Settings", expanded=False):
        if proxy_url:
            st.success(f"Proxy: {proxy_url}")
            use_proxy = st.checkbox("Use Proxy", value=True)
        else:
            st.warning("PMPROXY_URL not set")
            use_proxy = False

    # Store settings in session state - clear client cache if setting changed
    if st.session_state.get("use_proxy") != use_proxy:
        st.session_state.pop("client", None)
        st.session_state.pop("gamma", None)
        st.session_state.pop("clob", None)
    st.session_state["use_proxy"] = use_proxy

    # Render selected page
    if page == "🏦 Broker":
        from ui.broker import render_broker_page

        render_broker_page()
    elif page == "📈 Trading":
        from ui.trading import render_trading_page

        render_trading_page()
    elif page == "🔍 Scanner":
        st.title("🔍 Scanner")
        st.info("Scanner page coming soon...")
    elif page == "🤖 Bot":
        st.title("🤖 Bot")
        st.info("Bot page coming soon...")


def main():
    """Entry point for pmtrader-ui command."""
    sys.argv = ["streamlit", "run", __file__, "--server.headless=true"]
    sys.exit(stcli.main())


if __name__ == "__main__":
    # When run directly by streamlit, execute the app
    run_app()
