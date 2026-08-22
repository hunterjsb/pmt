//! SigV4 signing for pmproxy behind an AWS_IAM Function URL.
//!
//! The Function URL rejects unsigned requests account-wide (AuthType=NONE is
//! blocked), so every proxied CLOB call must carry a SigV4 signature computed
//! with the local AWS credentials. Cognito Bearer is retired — the Lambda runs
//! with PMPROXY_AUTH_ENABLED=false and IAM is the sole auth layer.
//!
//! Function URLs require the payload hash as an `x-amz-content-sha256` header,
//! not just inside the signature — without it AWS returns a bare 403.
//!
//! # Environment Variables
//!
//! - `PMPROXY_AWS_REGION`: signing region (default: eu-west-1)
//! - AWS credentials via the default chain (env vars, ~/.aws/credentials, ...)

use std::time::SystemTime;

use aws_credential_types::provider::ProvideCredentials;
use aws_sigv4::http_request::{
    sign, PayloadChecksumKind, SignableBody, SignableRequest, SigningSettings,
};
use aws_sigv4::sign::v4;
use aws_smithy_runtime_api::client::identity::Identity;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};

/// Signs pmproxy-bound requests with AWS credentials from the default chain.
///
/// Credentials are resolved once at construction (parity with pmtrader, which
/// freezes them at patch-install). Static IAM user keys don't expire; if we
/// ever move to session credentials this needs a refresh path.
pub struct SigV4Signer {
    identity: Identity,
    region: String,
}

impl SigV4Signer {
    /// Resolve credentials from the default AWS chain. Fails hard when none
    /// are found — an unsigned request to the proxy can only 403.
    pub async fn from_env() -> Result<Self, SigV4Error> {
        let region =
            std::env::var("PMPROXY_AWS_REGION").unwrap_or_else(|_| "eu-west-1".to_string());
        let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
            .region(aws_config::Region::new(region.clone()))
            .load()
            .await;
        let provider = config
            .credentials_provider()
            .ok_or_else(|| SigV4Error("no AWS credentials provider configured".to_string()))?;
        let creds = provider.provide_credentials().await.map_err(|e| {
            SigV4Error(format!(
                "AWS credentials required to sign pmproxy requests: {}",
                e
            ))
        })?;
        Ok(Self {
            identity: creds.into(),
            region,
        })
    }

    /// Compute the SigV4 headers for one request.
    ///
    /// `url` must be the exact final URL (query string included) and `body`
    /// the exact wire bytes — signed bytes must equal sent bytes. Returns the
    /// headers to attach: authorization, x-amz-date, x-amz-content-sha256,
    /// host. Any other request headers ride along unsigned.
    pub fn sign_headers(
        &self,
        method: &str,
        url: &str,
        body: &[u8],
    ) -> Result<HeaderMap, SigV4Error> {
        let mut settings = SigningSettings::default();
        settings.payload_checksum_kind = PayloadChecksumKind::XAmzSha256;

        let params: aws_sigv4::http_request::SigningParams = v4::SigningParams::builder()
            .identity(&self.identity)
            .region(&self.region)
            .name("lambda")
            .time(SystemTime::now())
            .settings(settings)
            .build()
            .map_err(|e| SigV4Error(format!("signing params: {}", e)))?
            .into();

        let signable =
            SignableRequest::new(method, url, std::iter::empty(), SignableBody::Bytes(body))
                .map_err(|e| SigV4Error(format!("signable request: {}", e)))?;

        let (instructions, _signature) = sign(signable, &params)
            .map_err(|e| SigV4Error(format!("signing failed: {}", e)))?
            .into_parts();

        let mut headers = HeaderMap::new();
        for (name, value) in instructions.headers() {
            let name = HeaderName::from_bytes(name.as_bytes())
                .map_err(|e| SigV4Error(format!("header name: {}", e)))?;
            let value = HeaderValue::from_str(value)
                .map_err(|e| SigV4Error(format!("header value: {}", e)))?;
            headers.insert(name, value);
        }
        Ok(headers)
    }
}

/// Errors from credential resolution or request signing.
#[derive(Debug)]
pub struct SigV4Error(pub String);

impl std::fmt::Display for SigV4Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "SigV4: {}", self.0)
    }
}

impl std::error::Error for SigV4Error {}
