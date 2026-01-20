use clap::{Parser, Subcommand};
use pmengine::{Config, Engine, GammaClient};
use rust_decimal_macros::dec;
use std::path::PathBuf;
use tracing::{info, Level};
use tracing_subscriber::FmtSubscriber;

#[derive(Parser, Debug)]
#[command(
    name = "pmengine",
    about = "Rust HFT engine for Polymarket trading"
)]
struct Cli {
    /// Log level (trace, debug, info, warn, error)
    #[arg(short, long, default_value = "info", global = true)]
    log_level: String,

    /// Path to .env file (default: searches for .env in current and parent directories)
    #[arg(long, global = true)]
    env_file: Option<PathBuf>,

    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Run one or more strategies
    Run {
        /// Strategy names to run (e.g., sure_bets market_maker)
        #[arg(required = true)]
        strategies: Vec<String>,

        /// Dry run mode - don't place real orders
        #[arg(long, default_value = "false")]
        dry_run: bool,

        /// Maximum number of ticks before automatic shutdown (0 = unlimited)
        #[arg(long, default_value = "0")]
        max_ticks: u64,

        /// Skip WebSocket warmup (useful when WS connection is unavailable)
        #[arg(long, default_value = "false")]
        skip_warmup: bool,
    },

    /// Test Gamma API only (no CLOB auth needed, prints discovered markets and exits)
    TestGamma,

    /// List available strategies
    List,
}

/// Load .env file, searching in current directory and parent directories up to 3 levels.
fn load_dotenv(explicit_path: Option<PathBuf>) {
    if let Some(path) = explicit_path {
        match dotenvy::from_path(&path) {
            Ok(_) => eprintln!("[pmengine] Loaded env from: {}", path.display()),
            Err(e) => eprintln!("[pmengine] Warning: Failed to load {}: {}", path.display(), e),
        }
        return;
    }

    // Search for .env in current directory and up to 3 parent directories
    let search_paths = [
        ".env",
        "../.env",
        "../../.env",
        "../../../.env",
    ];

    for relative_path in search_paths {
        if let Ok(path) = std::fs::canonicalize(relative_path) {
            if path.exists() {
                match dotenvy::from_path(&path) {
                    Ok(_) => {
                        eprintln!("[pmengine] Loaded env from: {}", path.display());
                        return;
                    }
                    Err(e) => {
                        eprintln!("[pmengine] Warning: Found {} but failed to load: {}", path.display(), e);
                    }
                }
            }
        }
    }

    // Also try the standard dotenvy search (which looks in CWD)
    if dotenvy::dotenv().is_ok() {
        eprintln!("[pmengine] Loaded env from current directory");
    } else {
        eprintln!("[pmengine] Note: No .env file found (this is OK if env vars are set)");
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();

    // Load .env file FIRST, before anything else needs env vars
    load_dotenv(cli.env_file.clone());

    // Set up logging
    let level = match cli.log_level.to_lowercase().as_str() {
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

    // Handle commands
    match cli.command {
        Some(Commands::TestGamma) => {
            run_test_gamma().await
        }
        Some(Commands::List) => {
            run_list()
        }
        Some(Commands::Run { strategies, dry_run, max_ticks, skip_warmup }) => {
            run_strategies(strategies, dry_run, max_ticks, skip_warmup).await
        }
        None => {
            eprintln!("Usage: pmengine <command>");
            eprintln!();
            eprintln!("Commands:");
            eprintln!("  run <strategies...>  Run one or more strategies");
            eprintln!("  list                 List available strategies");
            eprintln!("  test-gamma           Test Gamma API (no auth needed)");
            eprintln!();
            eprintln!("Examples:");
            eprintln!("  pmengine run sure_bets --dry-run");
            eprintln!("  pmengine run sure_bets market_maker --max-ticks 10");
            eprintln!("  pmengine list");
            Ok(())
        }
    }
}

async fn run_test_gamma() -> Result<(), Box<dyn std::error::Error>> {
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
            Ok(())
        }
        Err(e) => {
            tracing::error!("Gamma API test failed: {}", e);
            Err(e.to_string().into())
        }
    }
}

fn run_list() -> Result<(), Box<dyn std::error::Error>> {
    use pmengine::strategies::registry;

    println!("Available strategies:");
    println!();

    let reg = registry();
    let mut names: Vec<_> = reg.keys().collect();
    names.sort();

    for name in names {
        let info = reg.get(name).unwrap();
        let discovery = if info.requires_market_discovery {
            " [market-discovery]"
        } else {
            ""
        };
        println!("  {}{}", name, discovery);
    }

    println!();
    println!("Run with: pmengine run <strategy> [--dry-run] [--max-ticks N]");

    Ok(())
}

async fn run_strategies(
    strategy_names: Vec<String>,
    dry_run: bool,
    max_ticks: u64,
    skip_warmup: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    // Load configuration from environment
    let config = Config::from_env()?;
    info!("Configuration loaded");
    info!("  CLOB URL: {}", config.clob_url);
    info!("  Max position size: ${}", config.max_position_size);
    info!("  Max total exposure: ${}", config.max_total_exposure);
    info!("  Tick interval: {}ms", config.tick_interval_ms);

    // Create and run engine
    let mut engine = Engine::new(config, dry_run).await?;
    info!("Engine initialized");

    // Set skip warmup if requested
    if skip_warmup {
        engine.set_skip_warmup(true);
        info!("Warmup skipped (--skip-warmup)");
    }

    // Load strategies by name
    engine.load_strategies(&strategy_names)?;

    // Run the main event loop
    if max_ticks > 0 {
        info!("Running with max_ticks={}", max_ticks);
    }
    engine.run(max_ticks).await?;

    Ok(())
}
