use clap::Parser;
use pmengine::{Config, Engine, GammaClient};
use pmengine::strategies::{OrderTest, SpreadWatcher, SureBets};
use rust_decimal_macros::dec;
use tracing::{info, Level};
use tracing_subscriber::FmtSubscriber;

#[derive(Parser, Debug)]
#[command(
    name = "pmengine",
    about = "Rust HFT engine for Polymarket trading"
)]
struct Args {
    /// Log level (trace, debug, info, warn, error)
    #[arg(short, long, default_value = "info")]
    log_level: String,

    /// Dry run mode - don't place real orders
    #[arg(long, default_value = "false")]
    dry_run: bool,

    /// Run the order test strategy (places and cancels a small order)
    #[arg(long)]
    test_order: bool,

    /// Run the spread watcher strategy
    #[arg(long)]
    spread_watcher: bool,

    /// Run the sure bets strategy (high-certainty expiring markets)
    #[arg(long)]
    sure_bets: bool,

    /// Test Gamma API only (no CLOB auth needed, prints discovered markets and exits)
    #[arg(long)]
    test_gamma: bool,

    /// Scan-only mode - discover and print opportunities without trading
    #[arg(long)]
    scan_only: bool,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();

    // Set up logging
    let level = match args.log_level.to_lowercase().as_str() {
        "trace" => Level::TRACE,
        "debug" => Level::DEBUG,
        "info" => Level::INFO,
        "warn" => Level::WARN,
        "error" => Level::ERROR,
        _ => Level::INFO,
    };

    FmtSubscriber::builder()
        .with_max_level(level)
        .with_target(true)
        .with_thread_ids(true)
        .compact()
        .init();

    info!("pmengine starting...");

    // Handle --test-gamma: Test Gamma API only (no CLOB auth needed)
    if args.test_gamma {
        info!("Running Gamma API test mode...");
        let gamma = GammaClient::new();

        // Fetch sure bet candidates: expiring within 2 hours, 95%+ certainty
        match gamma.fetch_sure_bet_candidates(2.0, dec!(0.95)).await {
            Ok(markets) => {
                info!("Found {} sure bet candidates", markets.len());
                for market in &markets {
                    if let Some(hours) = market.hours_until_expiry() {
                        if let Some(idx) = market.highest_certainty_index() {
                            let price = market.outcome_prices.get(idx).copied().unwrap_or_default();
                            let outcome = market.outcomes.get(idx).cloned().unwrap_or_default();
                            info!(
                                "  {} | {} @ {:.2}¢ | {:.1}h left | slug: {}",
                                market.question,
                                outcome,
                                price * dec!(100),
                                hours,
                                market.slug
                            );
                        }
                    }
                }
                info!("Gamma API test completed successfully");
                return Ok(());
            }
            Err(e) => {
                tracing::error!("Gamma API test failed: {}", e);
                return Err(e.to_string().into());
            }
        }
    }

    // Load configuration from environment
    let config = Config::from_env()?;
    info!("Configuration loaded");
    info!("  CLOB URL: {}", config.clob_url);
    info!("  Max position size: ${}", config.max_position_size);
    info!("  Max total exposure: ${}", config.max_total_exposure);
    info!("  Tick interval: {}ms", config.tick_interval_ms);

    // Create and run engine
    let mut engine = Engine::new(config, args.dry_run).await?;
    info!("Engine initialized");

    // Register strategies
    if args.test_order {
        info!("Running order test strategy");
        engine.register_strategy(Box::new(OrderTest::new())).await;
    }
    if args.spread_watcher {
        info!("Running spread watcher strategy");
        engine.register_strategy(Box::new(SpreadWatcher::new())).await;
    }
    if args.sure_bets {
        info!("Running sure bets strategy");
        engine.enable_market_discovery();
        engine.register_strategy(Box::new(SureBets::new())).await;
    }

    // Run the main event loop
    engine.run().await?;

    Ok(())
}
