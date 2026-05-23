//! Polymarket SDK client wrapper with custom L2 auth for proxy compatibility.
//!
//! The official SDK computes HMAC signatures using the full URL path, which breaks
//! when using a reverse proxy with path prefixes (e.g., /clob/order instead of /order).
//!
//! This client:
//! 1. Uses the SDK for L1 auth (derive-api-key) and order signing
//! 2. Handles L2 authenticated requests (POST/DELETE order) ourselves with correct paths

use std::str::FromStr;

use alloy::hex::ToHexExt;
use alloy::primitives::{Address, U256};
use alloy::signers::local::LocalSigner;
use alloy::signers::Signer;
use base64::engine::general_purpose::URL_SAFE;
use base64::Engine;
use hmac::{Hmac, Mac};
use polymarket_client_sdk_v2::auth::Credentials;
use polymarket_client_sdk_v2::clob::client::{Client, Config as SdkConfig};
use polymarket_client_sdk_v2::clob::types::{Side as SdkSide, SignatureType};
use polymarket_client_sdk_v2::POLYGON;
use reqwest::header::{HeaderMap, HeaderValue};
use rust_decimal::Decimal;
use secrecy::ExposeSecret;
use sha2::Sha256;

use crate::config::Config;

use std::sync::Arc;
#[cfg(feature = "cognito")]
use crate::cognito::CognitoAuth;

/// Authenticated Polymarket client.
pub struct PolymarketClient {
    /// SDK client for order building/signing
    inner: Client<polymarket_client_sdk_v2::auth::state::Authenticated<polymarket_client_sdk_v2::auth::Normal>>,
    /// Signer for order signatures
    signer: LocalSigner<alloy::signers::k256::ecdsa::SigningKey>,
    /// L2 credentials from derive-api-key
    credentials: Credentials,
    /// Signer address (used in L2 headers)
    address: Address,
    /// HTTP client for L2 requests
    http: reqwest::Client,
    /// Proxy URL base (without /clob/ suffix)
    proxy_url: Option<String>,
    /// Dry run mode
    dry_run: bool,
    /// Optional Cognito auth for pmproxy multi-tenant auth
    #[cfg(feature = "cognito")]
    cognito_auth: Option<Arc<CognitoAuth>>,
    /// Cached tick-size decimal places per token. Lets us round each
    /// market's prices to its actual tick (0.001 vs 0.01 vs 0.0001)
    /// instead of the previous hard-coded 2-decimal rounding that
    /// silently snapped $0.9425 → $0.94 and killed quote precision.
    tick_decimals: Arc<tokio::sync::RwLock<std::collections::HashMap<String, u32>>>,
}

impl PolymarketClient {
    /// Create and authenticate a new client.
    #[cfg(not(feature = "cognito"))]
    pub async fn new(config: &Config, dry_run: bool) -> Result<Self, ClientError> {
        Self::new_internal(config, dry_run).await
    }

    /// Create and authenticate a new client with optional Cognito auth.
    #[cfg(feature = "cognito")]
    pub async fn new(config: &Config, dry_run: bool) -> Result<Self, ClientError> {
        Self::new_with_cognito(config, dry_run, None).await
    }

    /// Create and authenticate a new client with Cognito auth.
    #[cfg(feature = "cognito")]
    pub async fn new_with_cognito(
        config: &Config,
        dry_run: bool,
        cognito_auth: Option<Arc<CognitoAuth>>,
    ) -> Result<Self, ClientError> {
        let mut client = Self::new_internal(config, dry_run).await?;
        client.cognito_auth = cognito_auth;
        Ok(client)
    }

    /// Internal constructor shared by all public constructors.
    async fn new_internal(config: &Config, dry_run: bool) -> Result<Self, ClientError> {
        // Create signer from private key
        let signer = LocalSigner::from_str(&config.private_key)
            .map_err(|e| ClientError::InvalidPrivateKey(e.to_string()))?
            .with_chain_id(Some(POLYGON));

        // Determine signature type
        let sig_type = match config.signature_type {
            0 => SignatureType::Eoa,
            1 => SignatureType::Proxy,
            2 => SignatureType::GnosisSafe,
            _ => SignatureType::Eoa,
        };

        // Parse funder address if provided
        let funder: Option<Address> = config.funder_address.as_ref().and_then(|addr| {
            Address::from_str(addr).ok()
        });

        // Determine if we're using a proxy
        let proxy_url = std::env::var("PMPROXY_URL").ok();

        // Always authenticate directly with Polymarket (proxy doesn't support auth endpoints)
        let direct_clob_url = "https://clob.polymarket.com";

        // Build and authenticate client using direct URL
        let mut auth_builder = Client::new(direct_clob_url, SdkConfig::default())
            .map_err(|e| ClientError::SdkError(e.to_string()))?
            .authentication_builder(&signer)
            .signature_type(sig_type);

        // Set explicit funder if provided
        if let Some(f) = funder {
            auth_builder = auth_builder.funder(f);
        }

        let client = auth_builder
            .authenticate()
            .await
            .map_err(|e| ClientError::AuthError(e.to_string()))?;

        // Get credentials by doing another L1 auth call (SDK doesn't expose credentials after auth)
        // We use the unauthenticated client for this since derive_api_key uses L1 auth
        let unauth_client = Client::new(direct_clob_url, SdkConfig::default())
            .map_err(|e| ClientError::SdkError(e.to_string()))?;
        let credentials = unauth_client
            .derive_api_key(&signer, None)
            .await
            .map_err(|e| ClientError::AuthError(format!("Failed to get credentials: {}", e)))?;

        // The address used for L2 headers is always the signer (the key making the API call)
        let address = signer.address();

        // Create HTTP client for L2 requests
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
            .map_err(|e| ClientError::SdkError(e.to_string()))?;

        tracing::info!(
            signer = %signer.address(),
            funder = ?funder,
            address = %address,
            sig_type = ?sig_type,
            proxy = ?proxy_url,
            "Authenticated with Polymarket CLOB"
        );

        Ok(Self {
            inner: client,
            signer,
            credentials,
            address,
            http,
            proxy_url,
            dry_run,
            #[cfg(feature = "cognito")]
            cognito_auth: None,
            tick_decimals: Arc::new(tokio::sync::RwLock::new(std::collections::HashMap::new())),
        })
    }

    /// Look up (and cache) how many decimal places this market's tick size
    /// allows. Used by `place_limit_order` to round each price to the right
    /// precision instead of the hard-coded 2-decimal default.
    pub async fn tick_decimals_for(&self, token_id: &str) -> Result<u32, ClientError> {
        if let Some(d) = self.tick_decimals.read().await.get(token_id).copied() {
            return Ok(d);
        }
        let token_u256 = U256::from_str(token_id)
            .map_err(|e| ClientError::OrderError(format!("Invalid token_id: {}", e)))?;
        let resp = self
            .inner
            .tick_size(token_u256)
            .await
            .map_err(|e| ClientError::OrderError(format!("tick_size lookup failed: {}", e)))?;
        use polymarket_client_sdk_v2::clob::types::TickSize;
        let decimals = match resp.minimum_tick_size {
            TickSize::Tenth => 1,
            TickSize::Hundredth => 2,
            TickSize::Thousandth => 3,
            TickSize::TenThousandth => 4,
            _ => 2, // unknown variant — default to coarsest commonly safe value
        };
        self.tick_decimals
            .write()
            .await
            .insert(token_id.to_string(), decimals);
        Ok(decimals)
    }

    /// Compute L2 HMAC signature for a request.
    fn compute_l2_signature(&self, timestamp: i64, method: &str, path: &str, body: &str) -> Result<String, ClientError> {
        let message = format!("{}{}{}{}", timestamp, method, path, body);

        let secret_bytes = URL_SAFE
            .decode(self.credentials.secret().expose_secret())
            .map_err(|e| ClientError::OrderError(format!("Invalid secret encoding: {}", e)))?;

        let mut mac = Hmac::<Sha256>::new_from_slice(&secret_bytes)
            .map_err(|e| ClientError::OrderError(format!("HMAC error: {}", e)))?;
        mac.update(message.as_bytes());

        let result = mac.finalize();
        Ok(URL_SAFE.encode(result.into_bytes()))
    }

    /// Create L2 auth headers for a request.
    fn create_l2_headers(&self, method: &str, path: &str, body: &str) -> Result<HeaderMap, ClientError> {
        let timestamp = chrono::Utc::now().timestamp();
        let signature = self.compute_l2_signature(timestamp, method, path, body)?;

        let mut headers = HeaderMap::new();
        headers.insert("POLY_ADDRESS", HeaderValue::from_str(&self.address.encode_hex_with_prefix())
            .map_err(|e| ClientError::OrderError(e.to_string()))?);
        headers.insert("POLY_API_KEY", HeaderValue::from_str(&self.credentials.key().to_string())
            .map_err(|e| ClientError::OrderError(e.to_string()))?);
        headers.insert("POLY_PASSPHRASE", HeaderValue::from_str(self.credentials.passphrase().expose_secret())
            .map_err(|e| ClientError::OrderError(e.to_string()))?);
        headers.insert("POLY_SIGNATURE", HeaderValue::from_str(&signature)
            .map_err(|e| ClientError::OrderError(e.to_string()))?);
        headers.insert("POLY_TIMESTAMP", HeaderValue::from_str(&timestamp.to_string())
            .map_err(|e| ClientError::OrderError(e.to_string()))?);

        Ok(headers)
    }

    /// Make an L2-authenticated POST request.
    #[allow(unused_mut)] // mut needed only when cognito feature is enabled
    async fn l2_post<T: serde::de::DeserializeOwned>(&self, path: &str, body: &impl serde::Serialize) -> Result<T, ClientError> {
        let body_str = serde_json::to_string(body)
            .map_err(|e| ClientError::OrderError(format!("JSON serialization failed: {}", e)))?;

        let mut headers = self.create_l2_headers("POST", path, &body_str)?;

        // Add Cognito auth header if using proxy with auth
        #[cfg(feature = "cognito")]
        if self.proxy_url.is_some() {
            if let Some(ref cognito) = self.cognito_auth {
                let auth_header = cognito.get_auth_header().await
                    .map_err(|e| ClientError::AuthError(format!("Cognito auth failed: {}", e)))?;
                headers.insert("Authorization", HeaderValue::from_str(&auth_header)
                    .map_err(|e| ClientError::OrderError(e.to_string()))?);
            }
        }

        // Determine URL: if using proxy, use proxy URL with /clob prefix; otherwise use CLOB directly
        // Note: We compute HMAC for the canonical path (/order), but send to proxy path (/clob/order)
        let url = if let Some(ref proxy) = self.proxy_url {
            format!("{}/clob{}", proxy.trim_end_matches('/'), path)
        } else {
            format!("https://clob.polymarket.com{}", path)
        };

        tracing::debug!(url = %url, path = %path, body_len = body_str.len(), "L2 POST request");

        let response = self.http
            .post(&url)
            .headers(headers)
            .header("Content-Type", "application/json")
            .body(body_str)
            .send()
            .await
            .map_err(|e| ClientError::OrderError(format!("Request failed: {}", e)))?;

        let status = response.status();
        let body = response.text().await
            .map_err(|e| ClientError::OrderError(format!("Failed to read response: {}", e)))?;

        if !status.is_success() {
            return Err(ClientError::OrderError(format!("HTTP {}: {}", status, body)));
        }

        serde_json::from_str(&body)
            .map_err(|e| ClientError::OrderError(format!("JSON parse error: {} (body: {})", e, body)))
    }

    /// Place a limit order.
    pub async fn place_limit_order(
        &self,
        token_id: &str,
        side: Side,
        price: Decimal,
        size: Decimal,
    ) -> Result<String, ClientError> {
        if self.dry_run {
            let fake_id = format!("dry_run_{}", chrono::Utc::now().timestamp_millis());
            tracing::info!(
                order_id = %fake_id,
                token_id = token_id,
                side = ?side,
                price = %price,
                size = %size,
                "[DRY RUN] Would place order"
            );
            return Ok(fake_id);
        }

        let sdk_side = match side {
            Side::Buy => SdkSide::Buy,
            Side::Sell => SdkSide::Sell,
        };

        // Parse token_id as U256
        let token_id_u256 = U256::from_str(token_id)
            .map_err(|e| ClientError::OrderError(format!("Invalid token_id: {}", e)))?;

        // Use SDK to build and sign order
        let order = self.inner
            .limit_order()
            .token_id(token_id_u256)
            .side(sdk_side)
            .price(price)
            .size(size)
            .build()
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;

        let signed = self.inner
            .sign(&self.signer, order)
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;

        // POST using our own L2 auth (with correct path for HMAC)
        let response: PostOrderResponse = self.l2_post("/order", &signed).await?;

        tracing::info!(
            order_id = %response.order_id,
            token_id = token_id,
            side = ?side,
            price = %price,
            size = %size,
            "Order placed"
        );

        Ok(response.order_id)
    }

    /// Cancel an order.
    pub async fn cancel_order(&self, order_id: &str) -> Result<(), ClientError> {
        if self.dry_run {
            tracing::info!(order_id = order_id, "[DRY RUN] Would cancel order");
            return Ok(());
        }

        // Use SDK for cancel (it should work through proxy since it's a simpler operation)
        self.inner
            .cancel_order(order_id)
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;

        tracing::info!(order_id = order_id, "Order cancelled");
        Ok(())
    }

    /// Fetch all currently-open orders for the authenticated user.
    ///
    /// Pages through `/data/orders` until exhausted. Used at engine startup
    /// to repopulate the external-orders map with any orders left behind by
    /// a previous session (manual CLI placements, crashes, etc.) so they
    /// appear in `/orders/all` and can be cancelled via `CancelOrderById`.
    pub async fn get_open_orders(
        &self,
    ) -> Result<
        Vec<polymarket_client_sdk_v2::clob::types::response::OpenOrderResponse>,
        ClientError,
    > {
        use polymarket_client_sdk_v2::clob::types::request::OrdersRequest;
        let req = OrdersRequest::default();
        let mut all = Vec::new();
        let mut cursor: Option<String> = None;
        loop {
            let page = self
                .inner
                .orders(&req, cursor.clone())
                .await
                .map_err(|e| ClientError::OrderError(format!("orders fetch failed: {}", e)))?;
            all.extend(page.data);
            // SDK convention: empty cursor means no more pages. Polymarket
            // also uses "LTE=" as an end sentinel — treat both as terminal.
            if page.next_cursor.is_empty() || page.next_cursor == "LTE=" {
                break;
            }
            cursor = Some(page.next_cursor);
        }
        Ok(all)
    }

    /// Cancel multiple orders.
    pub async fn cancel_orders(&self, order_ids: &[&str]) -> Result<(), ClientError> {
        if self.dry_run {
            tracing::info!(count = order_ids.len(), "[DRY RUN] Would cancel orders");
            return Ok(());
        }

        self.inner
            .cancel_orders(order_ids)
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;

        tracing::info!(count = order_ids.len(), "Orders cancelled");
        Ok(())
    }

    /// Check if in dry run mode.
    pub fn is_dry_run(&self) -> bool {
        self.dry_run
    }

    /// Fetch the authenticated user's free USDC collateral balance, in USD.
    ///
    /// Polymarket returns the balance in raw USDC base units (6 decimals);
    /// we divide here so the value is on the same scale as trade notionals
    /// (`size * price`). The returned value reflects free cash — server-side
    /// already subtracts notional reserved by open orders.
    pub async fn get_collateral_balance(&self) -> Result<Decimal, ClientError> {
        use polymarket_client_sdk_v2::clob::types::AssetType;
        use polymarket_client_sdk_v2::clob::types::request::BalanceAllowanceRequest;
        let req = BalanceAllowanceRequest::builder()
            .asset_type(AssetType::Collateral)
            .build();
        let resp = self
            .inner
            .balance_allowance(req)
            .await
            .map_err(|e| ClientError::OrderError(format!("balance_allowance failed: {}", e)))?;
        // 1 USDC = 10^6 base units.
        Ok(resp.balance / Decimal::from(1_000_000u64))
    }

    /// Fetch the authenticated user's recent trades, newer than `after_ts`
    /// (unix seconds). Polymarket scopes this endpoint to the
    /// L2-authenticated user, so no maker/taker filter is needed.
    ///
    /// Used by the engine's trades-poller task to detect fills on
    /// engine-placed orders. The returned `TradeResponse` carries the order
    /// IDs (taker + maker side) and `fee_rate_bps` so the engine can compute
    /// the actual realized fee per fill.
    pub async fn get_user_trades_since(
        &self,
        after_ts: Option<i64>,
    ) -> Result<
        Vec<polymarket_client_sdk_v2::clob::types::response::TradeResponse>,
        ClientError,
    > {
        use polymarket_client_sdk_v2::clob::types::request::TradesRequest;
        let req = match after_ts {
            Some(ts) => TradesRequest::builder().after(ts).build(),
            None => TradesRequest::builder().build(),
        };
        let page = self
            .inner
            .trades(&req, None)
            .await
            .map_err(|e| ClientError::OrderError(format!("trades failed: {}", e)))?;
        Ok(page.data)
    }

    /// Fetch public market trades from Polymarket's data API.
    ///
    /// Returns trades on either token of the market (both YES and NO sides);
    /// callers filter by `MarketTrade.asset` to demultiplex back to the
    /// specific token they care about. Goes direct to `data-api.polymarket.com`
    /// — this endpoint is unauthenticated and not proxied through pmproxy.
    ///
    /// `after_ts` is a unix-seconds cursor: only trades strictly newer than
    /// it are returned. `None` returns the most recent batch.
    pub async fn get_market_trades_since(
        &self,
        condition_id: &str,
        after_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<MarketTrade>, ClientError> {
        let mut url = format!(
            "https://data-api.polymarket.com/trades?market={}&limit={}",
            condition_id, limit
        );
        if let Some(ts) = after_ts {
            url.push_str(&format!("&after={}", ts));
        }
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .map_err(|e| ClientError::OrderError(format!("public trades request failed: {}", e)))?;
        if !resp.status().is_success() {
            return Err(ClientError::OrderError(format!(
                "public trades HTTP {}",
                resp.status()
            )));
        }
        let trades: Vec<MarketTrade> = resp
            .json()
            .await
            .map_err(|e| ClientError::OrderError(format!("public trades parse: {}", e)))?;
        Ok(trades)
    }

    /// Cancel every open user-side order on a token (asset_id).
    ///
    /// Used by the engine at startup to clear orphans left from a previous
    /// session. Goes through the SDK's `cancel_market_orders` endpoint,
    /// which Polymarket scopes to the authenticated user, so it cannot
    /// touch other accounts' orders.
    pub async fn cancel_all_orders_on_token(&self, token_id: &str) -> Result<usize, ClientError> {
        if self.dry_run {
            tracing::info!(token_id = token_id, "[DRY RUN] Would cancel all orders on token");
            return Ok(0);
        }

        let token_u256 = U256::from_str(token_id)
            .map_err(|e| ClientError::OrderError(format!("Invalid token_id: {}", e)))?;

        let request = polymarket_client_sdk_v2::clob::types::request::CancelMarketOrderRequest::builder()
            .asset_id(token_u256)
            .build();

        let response = self
            .inner
            .cancel_market_orders(&request)
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;

        let count = response.canceled.len();
        tracing::info!(
            token_id = token_id,
            cancelled = count,
            "Cancelled all open orders on token"
        );
        Ok(count)
    }

    /// Fetch a snapshot of the order book via REST.
    ///
    /// Routes through pmproxy when `PMPROXY_URL` is set (adding the Cognito
    /// Bearer header), otherwise hits `clob.polymarket.com` directly. The
    /// `/book` endpoint is unauthenticated from Polymarket's side, so we
    /// don't compute L2 headers.
    ///
    /// Used by the engine's REST polling task as an alternative to the WS
    /// orderbook subscription (which is geoblocked from US IPs without a
    /// WS-capable proxy).
    pub async fn get_book(
        &self,
        token_id: &str,
    ) -> Result<crate::orderbook::OrderBook, ClientError> {
        let url = if let Some(ref proxy) = self.proxy_url {
            format!(
                "{}/clob/book?token_id={}",
                proxy.trim_end_matches('/'),
                token_id
            )
        } else {
            format!("https://clob.polymarket.com/book?token_id={}", token_id)
        };

        let mut req = self.http.get(&url);

        #[cfg(feature = "cognito")]
        if self.proxy_url.is_some() {
            if let Some(ref cognito) = self.cognito_auth {
                let auth_header = cognito
                    .get_auth_header()
                    .await
                    .map_err(|e| ClientError::AuthError(format!("Cognito auth failed: {}", e)))?;
                req = req.header("Authorization", auth_header);
            }
        }

        let response = req
            .send()
            .await
            .map_err(|e| ClientError::OrderError(format!("get_book request failed: {}", e)))?;

        let status = response.status();
        let body = response.text().await.map_err(|e| {
            ClientError::OrderError(format!("get_book read failed: {}", e))
        })?;

        if !status.is_success() {
            return Err(ClientError::OrderError(format!(
                "get_book HTTP {}: {}",
                status, body
            )));
        }

        let snap: BookRestResponse = serde_json::from_str(&body).map_err(|e| {
            ClientError::OrderError(format!("get_book JSON parse failed: {} (body: {})", e, body))
        })?;

        Ok(snap.into_orderbook(token_id))
    }
}

/// REST `/book` response. Polymarket returns timestamp as a string of unix
/// millis and bids/asks each as `[{price, size}]` arrays.
#[derive(Debug, serde::Deserialize)]
struct BookRestResponse {
    #[serde(default)]
    timestamp: Option<String>,
    #[serde(default)]
    hash: Option<String>,
    #[serde(default)]
    bids: Vec<BookRestLevel>,
    #[serde(default)]
    asks: Vec<BookRestLevel>,
}

#[derive(Debug, serde::Deserialize)]
struct BookRestLevel {
    #[serde(with = "rust_decimal::serde::str")]
    price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    size: Decimal,
}

impl BookRestResponse {
    fn into_orderbook(self, token_id: &str) -> crate::orderbook::OrderBook {
        use crate::orderbook::{Level, OrderBook};

        // Polymarket REST returns bids ascending and asks descending. Both
        // need flipping so best (bid: highest, ask: lowest) is at index 0.
        let mut bids: Vec<Level> = self
            .bids
            .into_iter()
            .map(|l| Level {
                price: l.price,
                size: l.size,
            })
            .collect();
        bids.sort_by(|a, b| b.price.cmp(&a.price));

        let mut asks: Vec<Level> = self
            .asks
            .into_iter()
            .map(|l| Level {
                price: l.price,
                size: l.size,
            })
            .collect();
        asks.sort_by(|a, b| a.price.cmp(&b.price));

        let timestamp: i64 = self
            .timestamp
            .and_then(|s| s.parse().ok())
            .unwrap_or_else(|| chrono::Utc::now().timestamp_millis());

        OrderBook {
            token_id: token_id.to_string(),
            bids,
            asks,
            timestamp,
            hash: self.hash,
        }
    }
}

/// Response from posting an order.
#[derive(Debug, serde::Deserialize)]
struct PostOrderResponse {
    #[serde(alias = "orderID")]
    order_id: String,
    #[allow(dead_code)]
    success: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

/// One trade row from Polymarket's public `/trades` data API.
///
/// Field names mirror the wire shape; only the fields the engine actually
/// uses are deserialized. `side` is the trader's side ("BUY"/"SELL");
/// `asset` is the CLOB token id; `condition_id` (alias for the wire's
/// `conditionId`) is the market id.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct MarketTrade {
    pub asset: String,
    #[serde(rename = "conditionId")]
    pub condition_id: String,
    pub side: String,
    #[serde(with = "rust_decimal::serde::float")]
    pub size: Decimal,
    #[serde(with = "rust_decimal::serde::float")]
    pub price: Decimal,
    pub timestamp: i64,
    #[serde(rename = "transactionHash")]
    pub transaction_hash: String,
    #[serde(rename = "proxyWallet")]
    pub proxy_wallet: String,
}

impl MarketTrade {
    /// Stable dedup key. Polymarket doesn't return a trade id, but the
    /// (tx hash, asset, side, price, size, wallet) tuple uniquely
    /// identifies a fill within a tx (one tx may settle multiple
    /// maker/taker pairs).
    pub fn dedup_key(&self) -> String {
        format!(
            "{}|{}|{}|{}|{}|{}",
            self.transaction_hash,
            self.asset,
            self.side,
            self.price,
            self.size,
            self.proxy_wallet
        )
    }
}

#[derive(Debug)]
pub enum ClientError {
    InvalidPrivateKey(String),
    AuthError(String),
    SdkError(String),
    OrderError(String),
    WebSocketError(String),
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientError::InvalidPrivateKey(e) => write!(f, "Invalid private key: {}", e),
            ClientError::AuthError(e) => write!(f, "Authentication error: {}", e),
            ClientError::SdkError(e) => write!(f, "SDK error: {}", e),
            ClientError::OrderError(e) => write!(f, "Order error: {}", e),
            ClientError::WebSocketError(e) => write!(f, "WebSocket error: {}", e),
        }
    }
}

impl std::error::Error for ClientError {}
