use clap::Parser;
use pmengine::{Config, Engine};
use pmengine::strategies::{OrderTest, SpreadWatcher, SureBets};
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
