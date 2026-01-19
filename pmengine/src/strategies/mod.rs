//! Auto-generated strategies from pmstrat.
//!
//! Add new strategies by running `pmstrat transpile` and adding them here.

mod order_test;
mod spread_watcher;
mod sure_bets;

pub use order_test::OrderTest;
pub use spread_watcher::SpreadWatcher;
pub use sure_bets::SureBets;
