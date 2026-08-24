//! Polymarket SDK client wrapper with custom L2 auth for proxy compatibility.
//!
//! The official SDK computes HMAC signatures using the full URL path, which breaks
//! when using a reverse proxy with path prefixes (e.g., /clob/order instead of /order).
//!
//! This client:
//! 1. Uses the SDK for L1 auth (derive-api-key) and order signing
//! 2. Handles L2 authenticated POSTs (place order) ourselves with correct paths.
//!    Cancels still go through the SDK — see `cancel_order`.

use std::str::FromStr;

use alloy::hex::ToHexExt;
use alloy::primitives::{Address, U256};
use alloy::signers::local::LocalSigner;
use alloy::signers::Signer;
use base64::engine::general_purpose::URL_SAFE;
use base64::Engine;
use hmac::{Hmac, KeyInit, Mac};
use polymarket_client_sdk_v2::auth::Credentials;
use polymarket_client_sdk_v2::clob::client::{Client, Config as SdkConfig};
use polymarket_client_sdk_v2::clob::types::{Side as SdkSide, SignatureType};
use polymarket_client_sdk_v2::POLYGON;
use reqwest::header::{HeaderMap, HeaderValue};
use rust_decimal::Decimal;
use secrecy::ExposeSecret;
use sha2::Sha256;

use crate::config::Config;
use crate::order_tape::OrderTimings;

use std::sync::Arc;
#[cfg(feature = "sigv4")]
use crate::sigv4::SigV4Signer;

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
    /// Funder address (the wallet that actually holds positions/USDC).
    /// Stored so the data-api position reconcile can query the right user.
    funder_address: Option<String>,
    /// Dry run mode
    dry_run: bool,
    /// Optional SigV4 signer for pmproxy behind an AWS_IAM Function URL
    #[cfg(feature = "sigv4")]
    sigv4: Option<Arc<SigV4Signer>>,
    /// Static per-token market metadata, resolved once and kept forever.
    meta: Arc<TokenMetaCache>,
}

/// The market facts an order needs that never change for a given token.
///
/// Both were being fetched from `clob.polymarket.com` on the order path —
/// the tick size twice over (once by us to round the price, again inside
/// the SDK's `build()`), the neg-risk flag inside `sign()`. Three serial
/// round trips at ~119ms each, which is most of the ~334ms local residual
/// in `analysis/latency_report.txt` section 7.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenMeta {
    /// Decimal places this market's tick size allows — prices round to
    /// their own market's tick, never a fixed 2 (docs/LESSONS.md#L3).
    pub tick_decimals: u32,
    /// Whether the token settles through the NegRisk adapter. Decides which
    /// exchange contract the EIP-712 domain signs against, so `sign()`
    /// cannot proceed without it.
    pub neg_risk: bool,
}

/// Outcome of a batch cancel, one entry per requested order.
#[derive(Debug, Default, Clone)]
pub struct CancelReport {
    /// Orders the CLOB confirmed are off the book.
    pub cancelled: Vec<String>,
    /// order id -> the reason the CLOB refused. Read each one through
    /// [`classify_cancel_refusal`]: a refusal is NOT automatically a live
    /// order, and treating it as one is what fed the stale-cancel storm.
    pub failed: std::collections::HashMap<String, String>,
}

/// What a refused cancel says about the order's fate.
///
/// The distinction the engine was missing. `CancelReport::failed` used to be
/// read as "still live, try again next tick", but the overwhelming majority
/// of refusals are the exchange saying the order is *already gone* — which is
/// the outcome the cancel wanted. Re-asking cannot change that answer, so a
/// 50 ms tick loop turns one dead order into a permanent CLOB round trip:
/// `analysis/watch_load.md` §2.4 measured 3514 refusals in one instance, and
/// the 2026-08-24 00:09Z log has 195 dead ids refused 16,028 times, every one
/// of them the same string.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CancelRefusal {
    /// The order is not on the book and no future request will change that:
    /// already cancelled, already matched/filled, or unknown to the exchange.
    /// A cancel answered this way SUCCEEDED — the order is off the book,
    /// which is all a cancel ever wanted. It must leave engine tracking now
    /// and never be re-attempted.
    Terminal,
    /// The exchange did not answer the question — auth, rate limit, 5xx,
    /// timeout, or anything we do not recognise. The order may well still be
    /// resting, so it stays tracked and gets tried again on a backoff.
    Transient,
}

/// Terminal refusal families, matched case-insensitively as substrings of
/// whatever the CLOB (or the SDK wrapping it) put in the message.
///
/// Substring matching rather than an exact table because this text is not a
/// stable API — Polymarket's live string today is
/// `order can't be found - already canceled or matched`, and the SDK prefixes
/// its own wrapper onto errors from the single-cancel path. The families are
/// narrow enough that a transient failure cannot land in one: nothing about a
/// 502 or a nonce error says an order does not exist.
const TERMINAL_CANCEL_MARKERS: &[&str] = &[
    "can't be found",
    "cant be found",
    "not found",
    "does not exist",
    "no such order",
    "unknown order",
    "already canceled",
    "already cancelled",
    "already matched",
    "already filled",
    "order is filled",
    "not cancellable",
];

/// Decide whether a refused cancel is done or worth retrying.
///
/// Unrecognised text is [`CancelRefusal::Transient`] on purpose: the failure
/// direction of a wrong guess is asymmetric. Calling a live order terminal
/// drops it from tracking while it still rests on the book — the ghost the
/// `cancel_all` fix exists to prevent. Calling a dead order transient only
/// costs one more round trip, and the backoff bounds even that.
pub fn classify_cancel_refusal(reason: &str) -> CancelRefusal {
    let lowered = reason.to_ascii_lowercase();
    if TERMINAL_CANCEL_MARKERS.iter().any(|m| lowered.contains(m)) {
        CancelRefusal::Terminal
    } else {
        CancelRefusal::Transient
    }
}

/// Per-token metadata cache. No TTL and no invalidation on purpose: a
/// token's tick size and neg-risk flag are fixed for the token's whole
/// life, so a refresh could only ever return the same answer. Memory is a
/// non-issue — a 5-15min window's churn leaves a few hundred entries a day.
#[derive(Default)]
pub struct TokenMetaCache {
    map: tokio::sync::RwLock<std::collections::HashMap<String, TokenMeta>>,
}

impl TokenMetaCache {
    pub fn new() -> Self {
        Self::default()
    }

    /// Cached value, or `None` on a cold token. Never touches the network.
    pub async fn get(&self, token_id: &str) -> Option<TokenMeta> {
        self.map.read().await.get(token_id).copied()
    }

    /// Cache-first resolution: a hit never calls `fetch`, and a value once
    /// stored is never fetched again.
    ///
    /// Two simultaneous misses on the same token both fetch — the lookups
    /// are idempotent GETs and `prewarm_token` makes the race rare, so a
    /// per-key lock would buy nothing but a way to deadlock the order path.
    ///
    /// Generic over the fetch so the never-refetch contract is unit-testable
    /// without a live CLOB.
    pub async fn resolve<F, Fut, E>(&self, token_id: &str, fetch: F) -> Result<TokenMeta, E>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<TokenMeta, E>>,
    {
        if let Some(meta) = self.get(token_id).await {
            return Ok(meta);
        }
        let meta = fetch().await?;
        self.map.write().await.insert(token_id.to_string(), meta);
        Ok(meta)
    }

    /// Number of tokens resolved so far.
    pub async fn len(&self) -> usize {
        self.map.read().await.len()
    }

    pub async fn is_empty(&self) -> bool {
        self.len().await == 0
    }
}

/// How long an idle pooled connection survives before hyper reaps it.
///
/// reqwest's default is 90s. The order path is only ever fast because it
/// finds a WARM connection: `analysis/order_latency_eu.md` measures a cold
/// TCP+TLS handshake to the CLOB at ~18ms (1.7ms connect + 16ms TLS), which
/// an order would pay in full. Today nothing drops out of the pool only
/// because the REST book poller happens to run every 10s — an accident of
/// `PMENGINE_BOOK_POLL_SLOW_MS`, not a property of the order path. Five
/// minutes spans a whole quiet window between arms, so the first fire of a
/// new window meets an open connection whatever the poller is doing.
const POOL_IDLE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(300);

/// How often the idle HTTP/2 connection to the CLOB is pinged.
///
/// Two jobs, both about never discovering a dead socket with a live order.
/// It keeps the connection genuinely in use, so Cloudflare's own idle reaper
/// never closes it underneath us; and it makes hyper find a half-open
/// connection with a ping rather than with an order that then has to be
/// re-sent on a fresh handshake. 30s sits well inside any edge idle window.
const H2_KEEPALIVE_INTERVAL: std::time::Duration = std::time::Duration::from_secs(30);

/// How long an unanswered ping waits before the connection is declared dead.
/// Generous next to the ~30ms the CLOB round trip actually takes: this must
/// only fire on a genuinely gone connection, never on a slow one.
const H2_KEEPALIVE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

/// The order path's HTTP client.
///
/// Its settings are money-path invariants, pinned here rather than inherited
/// from reqwest's defaults: a default that changes under a version bump would
/// change order latency silently, and this is the one client whose round trip
/// is the trade. Built ONCE per engine (see `new_internal`) — a client built
/// per order would pool nothing and hand every fire a fresh handshake.
fn build_order_http_client() -> reqwest::Result<reqwest::Client> {
    order_http_builder().build()
}

/// The settings themselves, split from `build` so a test can read them back —
/// a built `reqwest::Client` does not expose its own transport config.
fn order_http_builder() -> reqwest::ClientBuilder {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        // Nagle would hold a small order POST waiting for more bytes that
        // never come. reqwest already defaults this on; the money path
        // states it rather than depending on that staying true.
        .tcp_nodelay(true)
        .pool_idle_timeout(POOL_IDLE_TIMEOUT)
        // The CLOB already speaks h2 (ALPN negotiates it today), so the
        // engine holds ONE multiplexed connection — which makes that single
        // connection's liveness the whole order path's liveness.
        .http2_keep_alive_interval(H2_KEEPALIVE_INTERVAL)
        .http2_keep_alive_timeout(H2_KEEPALIVE_TIMEOUT)
        // Pinging only while there is traffic would defend exactly the case
        // that is already fine. The gap between two fires is when the
        // connection dies, so the pings have to run through it.
        .http2_keep_alive_while_idle(true)
}

impl PolymarketClient {
    /// Create and authenticate a new client.
    #[cfg(not(feature = "sigv4"))]
    pub async fn new(config: &Config, dry_run: bool) -> Result<Self, ClientError> {
        Self::new_internal(config, dry_run).await
    }

    // NOTE: no `new()` under `sigv4`. It existed, forwarded to
    // new_with_sigv4(.., None), and had zero callers in the only configuration
    // that ships a binary — while reading as the obvious constructor that
    // silently hands back an UNSIGNED client. Pick new_with_sigv4 explicitly.

    /// Create and authenticate a new client with SigV4 signing for pmproxy.
    #[cfg(feature = "sigv4")]
    pub async fn new_with_sigv4(
        config: &Config,
        dry_run: bool,
        sigv4: Option<Arc<SigV4Signer>>,
    ) -> Result<Self, ClientError> {
        let mut client = Self::new_internal(config, dry_run).await?;
        client.sigv4 = sigv4;
        Ok(client)
    }

    /// Internal constructor shared by all public constructors.
    async fn new_internal(config: &Config, dry_run: bool) -> Result<Self, ClientError> {
        // Create signer from private key
        let signer = LocalSigner::from_str(&config.private_key)
            .map_err(|e| ClientError::InvalidPrivateKey(e.to_string()))?
            .with_chain_id(Some(POLYGON));

        // Determine signature type. 3 = POLY_1271: a post-2026-05-04 deposit
        // wallet, where the funder is a CONTRACT that validates the order
        // through EIP-1271 rather than an address the EOA owns. Funder plumbing
        // is identical to 1/2 (maker = funder); what differs is inside the
        // signature — see `docs/deposit-wallet.md`.
        let sig_type = match config.signature_type {
            0 => SignatureType::Eoa,
            1 => SignatureType::Proxy,
            2 => SignatureType::GnosisSafe,
            3 => SignatureType::Poly1271,
            other => {
                tracing::warn!(
                    value = other,
                    "unknown PM_SIGNATURE_TYPE — falling back to EOA (0)"
                );
                SignatureType::Eoa
            }
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
        let http = build_order_http_client()
            .map_err(|e| ClientError::SdkError(e.to_string()))?;

        // Resolve the CLOB protocol version once, here. The SDK caches it
        // process-wide but resolves it lazily inside the FIRST order's
        // `build()`, where it costs that order a full round trip.
        if let Err(e) = client.version().await {
            tracing::warn!(error = %e, "CLOB version probe failed — the first order will resolve it");
        }

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
            funder_address: config.funder_address.clone(),
            dry_run,
            #[cfg(feature = "sigv4")]
            sigv4: None,
            meta: Arc::new(TokenMetaCache::new()),
        })
    }

    /// Resolve a token's static market metadata, from cache when possible.
    ///
    /// The two lookups the order path needs are independent GETs to the same
    /// host, so a miss fetches them CONCURRENTLY — one round trip of wall
    /// time, not two. Both calls also prime the SDK's own internal caches,
    /// which is what stops `build()` refetching the tick size and `sign()`
    /// refetching the neg-risk flag a few microseconds later.
    pub async fn token_meta(&self, token_id: &str) -> Result<TokenMeta, ClientError> {
        let token_u256 = U256::from_str(token_id)
            .map_err(|e| ClientError::OrderError(format!("Invalid token_id: {}", e)))?;
        self.meta
            .resolve(token_id, || self.fetch_token_meta(token_u256))
            .await
    }

    async fn fetch_token_meta(&self, token_u256: U256) -> Result<TokenMeta, ClientError> {
        let (tick, neg) = tokio::try_join!(
            self.inner.tick_size(token_u256),
            self.inner.neg_risk(token_u256),
        )
        .map_err(|e| ClientError::OrderError(format!("market meta lookup failed: {}", e)))?;

        use polymarket_client_sdk_v2::clob::types::TickSize;
        let tick_decimals = match tick.minimum_tick_size {
            TickSize::Tenth => 1,
            TickSize::Hundredth => 2,
            TickSize::Thousandth => 3,
            TickSize::TenThousandth => 4,
            _ => 2, // unknown variant — default to coarsest commonly safe value
        };
        Ok(TokenMeta {
            tick_decimals,
            neg_risk: neg.neg_risk,
        })
    }

    /// Resolve a token's metadata BEFORE any order needs it.
    ///
    /// Called when the engine subscribes a token, which happens minutes
    /// ahead of the first clip (report section 4: window open -> first fire
    /// is p10 150s). Cost lands there instead of inside decision->ack.
    /// Best-effort: a failure just leaves the order path to fetch it cold,
    /// exactly as it did before.
    pub async fn prewarm_token(&self, token_id: &str) {
        match self.token_meta(token_id).await {
            Ok(meta) => {
                // A V1 CLOB also resolves a per-token fee rate inside
                // `build()`; V2 carries fees in the market info instead.
                if self.inner.version().await.unwrap_or(2) == 1 {
                    if let Ok(t) = U256::from_str(token_id) {
                        if let Err(e) = self.inner.fee_rate_bps(t).await {
                            tracing::debug!(error = %e, token_id, "fee-rate prewarm failed");
                        }
                    }
                }
                tracing::debug!(
                    token_id,
                    tick_decimals = meta.tick_decimals,
                    neg_risk = meta.neg_risk,
                    "Prewarmed token metadata"
                );
            }
            Err(e) => {
                tracing::warn!(error = %e, token_id, "Token metadata prewarm failed");
            }
        }
    }

    /// How many decimal places this market's tick size allows. Cache read
    /// after the token's first touch — see `token_meta`.
    pub async fn tick_decimals_for(&self, token_id: &str) -> Result<u32, ClientError> {
        Ok(self.token_meta(token_id).await?.tick_decimals)
    }

    /// The token's tick if something has already resolved it, `None` if not.
    /// Cache read only — this never touches the network.
    ///
    /// The delta matcher needs the real tick to compare a desired quote
    /// against a standing one in wire units, and it runs inside the
    /// decision->ack window that `prewarm_token` exists to keep off the
    /// wire: `tick_decimals_for` would spend a ~119ms round trip there on a
    /// miss. Every token carrying a live order has already been through
    /// `token_meta` on the order path, so a miss here means a genuinely
    /// cold token and the caller's conservative fallback is the right answer.
    pub async fn cached_tick_decimals(&self, token_id: &str) -> Option<u32> {
        self.meta.get(token_id).await.map(|m| m.tick_decimals)
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
    ///
    /// `send_at` is stamped the instant the request is handed to the socket,
    /// so the Phase 7 tape can separate header/signing work from wire time.
    #[allow(unused_mut)] // mut needed only when sigv4 feature is enabled
    async fn l2_post<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        body: &impl serde::Serialize,
        send_at: &mut Option<std::time::Instant>,
    ) -> Result<T, ClientError> {
        let body_str = serde_json::to_string(body)
            .map_err(|e| ClientError::OrderError(format!("JSON serialization failed: {}", e)))?;

        // Determine URL: if using proxy, use proxy URL with /clob prefix; otherwise use CLOB directly
        // Note: We compute HMAC for the canonical path (/order), but send to proxy path (/clob/order)
        let url = if let Some(ref proxy) = self.proxy_url {
            format!("{}/clob{}", proxy.trim_end_matches('/'), path)
        } else {
            format!("https://clob.polymarket.com{}", path)
        };

        let mut headers = self.create_l2_headers("POST", path, &body_str)?;

        // Sign for the IAM Function URL when routed through the proxy. Signed
        // bytes must equal wire bytes, so sign the final URL + serialized body;
        // the POLY_* L2 headers ride along unsigned.
        #[cfg(feature = "sigv4")]
        if self.proxy_url.is_some() {
            if let Some(ref signer) = self.sigv4 {
                let signed = signer
                    .sign_headers("POST", &url, body_str.as_bytes())
                    .map_err(|e| ClientError::AuthError(e.to_string()))?;
                headers.extend(signed);
            }
        }

        tracing::debug!(url = %url, path = %path, body_len = body_str.len(), "L2 POST request");

        *send_at = Some(std::time::Instant::now());
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

    /// Place a limit order (crossing allowed — the taker path).
    pub async fn place_limit_order(
        &self,
        token_id: &str,
        side: Side,
        price: Decimal,
        size: Decimal,
    ) -> Result<String, ClientError> {
        self.place_limit_order_timed(token_id, side, price, size, false, &mut OrderTimings::start())
            .await
    }

    /// `place_limit_order`, stamping each stage into `t` for the Phase 7
    /// tape. The stamps are bare `Instant` reads — no allocation, no
    /// syscall past the clock — so measuring the hot path costs it nothing.
    ///
    /// `post_only` sets the CLOB's `postOnly` flag alongside the builder's
    /// default `GTC`. Polymarket REJECTS a post-only order that would match
    /// immediately rather than repricing it (docs/maker-design.md §2), so
    /// the caller must have clamped the price inside the book already — a
    /// bounced order spends rate-limit budget and returns nothing.
    pub async fn place_limit_order_timed(
        &self,
        token_id: &str,
        side: Side,
        price: Decimal,
        size: Decimal,
        post_only: bool,
        t: &mut OrderTimings,
    ) -> Result<String, ClientError> {
        if self.dry_run {
            let fake_id = format!("dry_run_{}", chrono::Utc::now().timestamp_millis());
            // Stamp every stage so a dry-run line is shaped like a real one
            // and honestly reports the ~0ms path it actually took.
            let now = std::time::Instant::now();
            t.sign_done = Some(now);
            t.send = Some(now);
            t.ack = Some(now);
            tracing::info!(
                order_id = %fake_id,
                token_id = token_id,
                side = ?side,
                price = %price,
                size = %size,
                post_only = post_only,
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

        // Use SDK to build and sign order. The builder's own defaults are
        // GTC + postOnly:false, and `SignedOrder` serializes `postOnly`
        // beside `order`/`orderType`/`owner` — so this only ever flips a
        // field the wire already carried, it does not add one.
        let mut builder = self.inner
            .limit_order()
            .token_id(token_id_u256)
            .side(sdk_side)
            .price(price)
            .size(size);
        if post_only {
            builder = builder.post_only(true);
        }
        let order = builder
            .build()
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;

        // `sign` resolves the token's neg-risk flag to pick the EIP-712
        // verifying contract — a cache read once `token_meta` has run.
        let signed = self.inner
            .sign(&self.signer, order)
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;
        t.sign_done = Some(std::time::Instant::now());

        // POST using our own L2 auth (with correct path for HMAC)
        let response: PostOrderResponse = self.l2_post("/order", &signed, &mut t.send).await?;
        t.ack = Some(std::time::Instant::now());

        tracing::info!(
            order_id = %response.order_id,
            token_id = token_id,
            side = ?side,
            price = %price,
            size = %size,
            post_only = post_only,
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

    /// Cancel multiple orders in ONE request.
    ///
    /// Polymarket answers per order, and the caller needs that granularity:
    /// marking a still-live order cancelled is the ghost `cancel_all` was
    /// fixed to avoid (docs/LESSONS.md#L6). A transport failure is still an
    /// `Err` — nothing was cancelled; a partial refusal comes back inside
    /// the report.
    pub async fn cancel_orders(&self, order_ids: &[&str]) -> Result<CancelReport, ClientError> {
        if self.dry_run {
            tracing::info!(count = order_ids.len(), "[DRY RUN] Would cancel orders");
            return Ok(CancelReport {
                cancelled: order_ids.iter().map(|s| s.to_string()).collect(),
                failed: std::collections::HashMap::new(),
            });
        }

        let resp = self
            .inner
            .cancel_orders(order_ids)
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;

        tracing::info!(
            requested = order_ids.len(),
            cancelled = resp.canceled.len(),
            refused = resp.not_canceled.len(),
            "Orders cancelled"
        );
        Ok(CancelReport {
            cancelled: resp.canceled,
            failed: resp.not_canceled,
        })
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

    /// Fetch the authoritative position size for a token from the data-api.
    ///
    /// Returns `(size, avg_price)` where size is signed (negative = short).
    /// `None` if we hold no position in that token, or no funder is set.
    ///
    /// This is the engine's source of truth for position reconciliation —
    /// incremental fill detection can miss fills (e.g. partials landing in
    /// the MM's cancel/replace window), so the engine periodically corrects
    /// its tracked size against this. Unauthenticated data-api endpoint, not
    /// proxied through pmproxy.
    pub async fn get_position(
        &self,
        token_id: &str,
    ) -> Result<Option<(Decimal, Decimal)>, ClientError> {
        let Some(funder) = self.funder_address.as_ref() else {
            return Ok(None);
        };
        let url = format!(
            "https://data-api.polymarket.com/positions?user={}&sizeThreshold=0",
            funder
        );
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .map_err(|e| ClientError::OrderError(format!("positions request failed: {}", e)))?;
        if !resp.status().is_success() {
            return Err(ClientError::OrderError(format!(
                "positions HTTP {}",
                resp.status()
            )));
        }
        let positions: Vec<serde_json::Value> = resp
            .json()
            .await
            .map_err(|e| ClientError::OrderError(format!("positions parse: {}", e)))?;
        for p in positions {
            if p.get("asset").and_then(|a| a.as_str()) == Some(token_id) {
                let size = p
                    .get("size")
                    .and_then(|s| s.as_f64())
                    .and_then(Decimal::from_f64_retain)
                    .unwrap_or(Decimal::ZERO);
                let avg = p
                    .get("avgPrice")
                    .and_then(|s| s.as_f64())
                    .and_then(Decimal::from_f64_retain)
                    .unwrap_or(Decimal::ZERO);
                return Ok(Some((size, avg)));
            }
        }
        Ok(None)
    }

    /// Every position in one fetch, keyed by asset (token_id) -> (size, avg).
    ///
    /// The endpoint only answers whole-account, so per-token reconcile loops
    /// calling get_position() N times re-download the same ~30KB body N times
    /// per pass — measured at 1.93GB/2h and 9s control-plane blackouts under
    /// load. A token absent from the map means the data-api reports no
    /// position, same as get_position's Ok(None): the caller skips it.
    pub async fn get_all_positions(
        &self,
    ) -> Result<std::collections::HashMap<String, (Decimal, Decimal)>, ClientError> {
        let Some(funder) = self.funder_address.as_ref() else {
            return Ok(std::collections::HashMap::new());
        };
        let url = format!(
            "https://data-api.polymarket.com/positions?user={}&sizeThreshold=0",
            funder
        );
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .map_err(|e| ClientError::OrderError(format!("positions request failed: {}", e)))?;
        if !resp.status().is_success() {
            return Err(ClientError::OrderError(format!(
                "positions HTTP {}",
                resp.status()
            )));
        }
        let positions: Vec<serde_json::Value> = resp
            .json()
            .await
            .map_err(|e| ClientError::OrderError(format!("positions parse: {}", e)))?;
        let mut out = std::collections::HashMap::new();
        for p in positions {
            let Some(asset) = p.get("asset").and_then(|a| a.as_str()) else {
                continue;
            };
            let size = p
                .get("size")
                .and_then(|s| s.as_f64())
                .and_then(Decimal::from_f64_retain)
                .unwrap_or(Decimal::ZERO);
            let avg = p
                .get("avgPrice")
                .and_then(|s| s.as_f64())
                .and_then(Decimal::from_f64_retain)
                .unwrap_or(Decimal::ZERO);
            out.insert(asset.to_string(), (size, avg));
        }
        Ok(out)
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
    /// Routes through pmproxy when `PMPROXY_URL` is set (SigV4-signing for
    /// the IAM Function URL), otherwise hits `clob.polymarket.com` directly.
    /// The `/book` endpoint is unauthenticated from Polymarket's side, so we
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

        #[cfg(feature = "sigv4")]
        if self.proxy_url.is_some() {
            if let Some(ref signer) = self.sigv4 {
                let signed = signer
                    .sign_headers("GET", &url, b"")
                    .map_err(|e| ClientError::AuthError(e.to_string()))?;
                req = req.headers(signed);
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
        bids.sort_by_key(|l| std::cmp::Reverse(l.price));

        let mut asks: Vec<Level> = self
            .asks
            .into_iter()
            .map(|l| Level {
                price: l.price,
                size: l.size,
            })
            .collect();
        asks.sort_by_key(|l| l.price);

        let timestamp: i64 = self
            .timestamp
            .and_then(|s| s.parse().ok())
            .unwrap_or_else(|| chrono::Utc::now().timestamp_millis());

        // Feed metadata is stamped by the hub when the snapshot is accepted
        // (`apply_rest_book`) — a snapshot that loses the staleness compare
        // never becomes a book, so it never gets a receipt time either.
        OrderBook {
            token_id: token_id.to_string(),
            bids,
            asks,
            timestamp,
            hash: self.hash,
            source: crate::orderbook::BookSource::Rest,
            received_at_ms: 0,
            update_count: 0,
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
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientError::InvalidPrivateKey(e) => write!(f, "Invalid private key: {}", e),
            ClientError::AuthError(e) => write!(f, "Authentication error: {}", e),
            ClientError::SdkError(e) => write!(f, "SDK error: {}", e),
            ClientError::OrderError(e) => write!(f, "Order error: {}", e),
        }
    }
}

impl std::error::Error for ClientError {}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// The string Polymarket actually returns, byte-for-byte off
    /// `~/.pmt/engine/engine-systemd.log` — all 16,028 refusals in the
    /// 2026-08-24 00:09Z window carried exactly this text.
    const LIVE_REFUSAL: &str = "order can't be found - already canceled or matched";

    #[test]
    fn the_refusal_the_storm_was_made_of_is_terminal() {
        assert_eq!(
            classify_cancel_refusal(LIVE_REFUSAL),
            CancelRefusal::Terminal,
            "the CLOB just said the order does not exist — asking again cannot change that",
        );
    }

    #[test]
    fn every_way_of_saying_the_order_is_gone_is_terminal() {
        for reason in [
            LIVE_REFUSAL,
            "order not found",
            "Order Not Found",
            "order does not exist",
            "no such order",
            "unknown order id",
            "order already cancelled",
            "already canceled",
            "order already matched",
            "already filled",
            // The SDK wraps the CLOB's text in its own error prose; the
            // marker has to survive being embedded, which is why the match
            // is a substring and not an equality.
            "Order error: SDK error: order can't be found - already canceled or matched",
        ] {
            assert_eq!(
                classify_cancel_refusal(reason),
                CancelRefusal::Terminal,
                "{reason:?}",
            );
        }
    }

    #[test]
    fn an_unanswered_cancel_is_transient_and_so_is_anything_unrecognised() {
        // The asymmetry that decides the default: a wrong Transient costs one
        // more round trip, a wrong Terminal abandons a live order on the book.
        for reason in [
            "502 Bad Gateway",
            "503 Service Unavailable",
            "request timed out",
            "connection reset by peer",
            "429 Too Many Requests",
            "invalid nonce",
            "unauthorized",
            "insufficient balance",
            "",
            "something nobody has seen before",
        ] {
            assert_eq!(
                classify_cancel_refusal(reason),
                CancelRefusal::Transient,
                "{reason:?}",
            );
        }
    }

    /// HTTP/1.1 stub that counts ACCEPTED CONNECTIONS, not requests.
    /// Connections are the only honest measure of pooling: the order path's
    /// cost is the handshake, and a handshake is exactly one accept.
    async fn counting_stub() -> (String, Arc<AtomicUsize>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind stub");
        let addr = listener.local_addr().expect("stub addr");
        let conns = Arc::new(AtomicUsize::new(0));
        let counter = conns.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    return;
                };
                counter.fetch_add(1, Ordering::SeqCst);
                tokio::spawn(async move {
                    let mut buf = [0u8; 2048];
                    loop {
                        match sock.read(&mut buf).await {
                            Ok(0) | Err(_) => return,
                            Ok(_) => {
                                let resp = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok";
                                if sock.write_all(resp).await.is_err() {
                                    return;
                                }
                            }
                        }
                    }
                });
            }
        });
        (format!("http://{addr}/order"), conns)
    }

    #[tokio::test]
    async fn the_order_client_reuses_one_connection_across_sequential_orders() {
        let (url, conns) = counting_stub().await;
        let http = build_order_http_client().expect("build order client");
        for _ in 0..12 {
            let resp = http.get(&url).send().await.expect("stub request");
            assert!(resp.status().is_success());
            // The body must be drained or the connection never returns to
            // the pool — a half-read response is a leaked connection.
            resp.text().await.expect("stub body");
        }
        assert_eq!(
            conns.load(Ordering::SeqCst),
            1,
            "every order after the first must ride the pooled connection; a \
             second accept is a TCP+TLS handshake charged to a live fire"
        );
    }

    #[tokio::test]
    async fn a_client_built_per_order_would_handshake_every_time() {
        // The regression the test above guards, demonstrated: pooling lives in
        // the CLIENT, so moving construction onto the order path defeats it
        // completely. This is what the old `reqwest::Client::new()`-per-call
        // shape costs, and why `new_internal` builds exactly one.
        let (url, conns) = counting_stub().await;
        for _ in 0..5 {
            let http = build_order_http_client().expect("build order client");
            let resp = http.get(&url).send().await.expect("stub request");
            resp.text().await.expect("stub body");
        }
        assert_eq!(
            conns.load(Ordering::SeqCst),
            5,
            "a per-order client cannot pool — if this ever passes at 1 the \
             pooling assertion above has stopped proving anything"
        );
    }

    /// reqwest's `Debug` does not surface the h2 keep-alive fields, so the
    /// wiring is checked by the compiler and what is asserted here is the part
    /// that can silently be WRONG: the relationship between the three
    /// durations. Each of these misorderings leaves the keep-alive doing
    /// nothing while still looking configured.
    #[test]
    fn the_keepalive_durations_cannot_defeat_each_other() {
        assert!(
            H2_KEEPALIVE_INTERVAL < POOL_IDLE_TIMEOUT,
            "pings slower than the pool reaper never refresh anything — the \
             connection is gone before the next ping is due"
        );
        assert!(
            H2_KEEPALIVE_TIMEOUT < H2_KEEPALIVE_INTERVAL,
            "a ping deadline that outlives the next ping can never fire, so a \
             dead connection is still found by an order"
        );
        // The CLOB round trip is ~30ms from the EU box. A deadline anywhere
        // near that would tear down healthy connections under a latency
        // spike, which costs a handshake instead of saving one.
        assert!(
            H2_KEEPALIVE_TIMEOUT >= std::time::Duration::from_secs(5),
            "keep-alive timeout is close enough to a normal round trip to \
             kill healthy connections"
        );
    }

    #[test]
    fn the_order_client_pins_tcp_nodelay() {
        // Nagle on a small order POST is pure added latency. reqwest defaults
        // this on today; the assertion is what makes it a pinned property of
        // the money path rather than a default we happen to inherit.
        let cfg = format!("{:?}", order_http_builder());
        assert!(
            cfg.contains("tcp_nodelay: true"),
            "order client lost TCP_NODELAY: {cfg}"
        );
    }

    fn meta(dp: u32) -> TokenMeta {
        TokenMeta {
            tick_decimals: dp,
            neg_risk: false,
        }
    }

    #[tokio::test]
    async fn cold_token_fetches_once_then_never_again() {
        let cache = TokenMetaCache::new();
        let fetches = AtomicUsize::new(0);
        let fetch = || async {
            fetches.fetch_add(1, Ordering::SeqCst);
            Ok::<_, ()>(meta(3))
        };

        assert_eq!(cache.resolve("tok", fetch).await, Ok(meta(3)));
        assert_eq!(fetches.load(Ordering::SeqCst), 1, "the cold miss must fetch");

        for _ in 0..50 {
            assert_eq!(cache.resolve("tok", fetch).await, Ok(meta(3)));
        }
        assert_eq!(
            fetches.load(Ordering::SeqCst),
            1,
            "a cached token must never be refetched — tick size and neg-risk are fixed for its life"
        );
    }

    #[tokio::test]
    async fn distinct_tokens_do_not_share_an_entry() {
        let cache = TokenMetaCache::new();
        cache
            .resolve("a", || async { Ok::<_, ()>(meta(2)) })
            .await
            .unwrap();
        cache
            .resolve("b", || async { Ok::<_, ()>(meta(4)) })
            .await
            .unwrap();
        assert_eq!(cache.get("a").await, Some(meta(2)));
        assert_eq!(cache.get("b").await, Some(meta(4)));
        assert_eq!(cache.len().await, 2);
    }

    #[tokio::test]
    async fn a_failed_fetch_is_not_cached() {
        let cache = TokenMetaCache::new();
        assert!(cache.is_empty().await);
        let err: Result<TokenMeta, &str> =
            cache.resolve("tok", || async { Err("clob down") }).await;
        assert_eq!(err, Err("clob down"));
        assert_eq!(
            cache.get("tok").await,
            None,
            "a failed lookup must leave the token cold, not poison it"
        );
        // The next attempt still resolves normally.
        assert_eq!(
            cache
                .resolve("tok", || async { Ok::<_, &str>(meta(2)) })
                .await,
            Ok(meta(2))
        );
    }

    #[tokio::test]
    async fn get_never_fetches() {
        let cache = TokenMetaCache::new();
        assert_eq!(cache.get("never-seen").await, None);
        assert!(cache.is_empty().await);
    }
}
