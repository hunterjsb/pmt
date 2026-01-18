//! Polymarket SDK client wrapper.

use std::str::FromStr;

use alloy::primitives::U256;
use alloy::signers::local::LocalSigner;
use alloy::signers::Signer;
use polymarket_client_sdk::clob::client::{Client, Config as SdkConfig};
use polymarket_client_sdk::clob::types::{Side as SdkSide, SignatureType};
use polymarket_client_sdk::POLYGON;
use rust_decimal::Decimal;

use crate::config::Config;

/// Authenticated Polymarket client.
pub struct PolymarketClient {
    inner: Client<polymarket_client_sdk::auth::state::Authenticated<polymarket_client_sdk::auth::Normal>>,
    signer: LocalSigner<alloy::signers::k256::ecdsa::SigningKey>,
    dry_run: bool,
}

impl PolymarketClient {
    /// Create and authenticate a new client.
    pub async fn new(config: &Config, dry_run: bool) -> Result<Self, ClientError> {
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

        // Build and authenticate client
        let client = Client::new(&config.clob_url, SdkConfig::default())
            .map_err(|e| ClientError::SdkError(e.to_string()))?
            .authentication_builder(&signer)
            .signature_type(sig_type)
            .authenticate()
            .await
            .map_err(|e| ClientError::AuthError(e.to_string()))?;

        tracing::info!(
            address = %signer.address(),
            sig_type = ?sig_type,
            "Authenticated with Polymarket CLOB"
        );

        Ok(Self { inner: client, signer, dry_run })
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

        // Parse token_id as U256 (hex string)
        let token_id_u256 = U256::from_str(token_id)
            .map_err(|e| ClientError::OrderError(format!("Invalid token_id: {}", e)))?;

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

        let response = self.inner
            .post_order(signed)
            .await
            .map_err(|e| ClientError::OrderError(e.to_string()))?;

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
