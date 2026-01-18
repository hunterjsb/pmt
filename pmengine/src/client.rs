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
use polymarket_client_sdk::auth::Credentials;
use polymarket_client_sdk::clob::client::{Client, Config as SdkConfig};
use polymarket_client_sdk::clob::types::{Side as SdkSide, SignatureType};
use polymarket_client_sdk::POLYGON;
use reqwest::header::{HeaderMap, HeaderValue};
use rust_decimal::Decimal;
use secrecy::ExposeSecret;
use sha2::Sha256;

use crate::config::Config;

#[cfg(feature = "cognito")]
use std::sync::Arc;
#[cfg(feature = "cognito")]
use crate::cognito::CognitoAuth;

/// Authenticated Polymarket client.
pub struct PolymarketClient {
    /// SDK client for order building/signing
    inner: Client<polymarket_client_sdk::auth::state::Authenticated<polymarket_client_sdk::auth::Normal>>,
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

        // Build and authenticate client
        let mut auth_builder = Client::new(&config.clob_url, SdkConfig::default())
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
        let unauth_client = Client::new(&config.clob_url, SdkConfig::default())
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
        })
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
