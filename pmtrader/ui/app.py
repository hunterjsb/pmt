"""Main Streamlit application entry point."""

import sys

import streamlit as st
from streamlit.web import cli as stcli


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
        ["🏦 Broker", "🔍 Scanner", "🤖 Bot"],
        label_visibility="collapsed",
    )

    st.sidebar.markdown("---")

    # Connection settings in sidebar
    with st.sidebar.expander("⚙️ Settings", expanded=False):
        use_proxy = st.checkbox("Use Proxy", value=False)
        proxy_url = st.text_input(
            "Proxy URL",
            value="http://localhost:8080",
            disabled=not use_proxy,
        )
        st.caption("Set PMPROXY_URL env var or configure here")

    # Store settings in session state
    st.session_state["use_proxy"] = use_proxy
    st.session_state["proxy_url"] = proxy_url

    # Render selected page
    if page == "🏦 Broker":
        from ui.broker import render_broker_page

        render_broker_page()
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
