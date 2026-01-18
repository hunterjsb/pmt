//! pmengine - Rust HFT engine for Polymarket trading.
//!
//! This crate provides a high-performance trading engine that:
//! - Connects to Polymarket via the official SDK
//! - Maintains local order book state from WebSocket streams
//! - Runs configurable trading strategies
//! - Manages orders with risk controls
//!
//! # Architecture
//!
//! The engine runs a `tokio::select!` event loop that processes:
//! - Tick timer events (strategy evaluation)
//! - WebSocket market data updates
//! - Order fill notifications
//!
//! Strategies generate signals that pass through risk management before execution.

pub mod client;
pub mod config;
pub mod engine;
pub mod order;
pub mod position;
pub mod risk;
pub mod strategy;
pub mod strategies;

#[cfg(feature = "cognito")]
pub mod cognito;

pub use client::{ClientError, PolymarketClient, Side};
pub use config::Config;
pub use engine::Engine;
pub use order::OrderManager;
pub use position::{Fill, Position, PositionTracker};
pub use risk::{RiskLimits, RiskManager};
pub use strategy::{Signal, Strategy, StrategyContext, StrategyRuntime, Urgency};

/// Re-export commonly used types from dependencies
pub mod prelude {
    pub use crate::{
        Config, Engine, Fill, OrderManager, Position, PositionTracker,
        RiskLimits, RiskManager, Signal, Strategy, StrategyContext, Urgency,
    };
    pub use rust_decimal::Decimal;
    pub use rust_decimal_macros::dec;
}
