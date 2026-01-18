//! Error types for authentication and rate limiting.

use axum::{
    body::Body,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use thiserror::Error;

/// Authentication and authorization errors.
#[derive(Debug, Error)]
pub enum AuthError {
    /// No Authorization header or Bearer token provided.
    #[error("Missing authentication token")]
    MissingToken,

    /// Token is malformed or signature is invalid.
    #[error("Invalid token: {0}")]
    InvalidToken(String),

    /// Token has expired.
    #[error("Token has expired")]
    ExpiredToken,

    /// Rate limit exceeded for this tenant.
    #[error("Rate limit exceeded")]
    RateLimited,

    /// Failed to fetch JWKS from Cognito.
    #[error("Failed to fetch JWKS: {0}")]
    JwksFetchError(String),
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            AuthError::MissingToken => (
                StatusCode::UNAUTHORIZED,
                "Missing or invalid Authorization header. Use: Authorization: Bearer <token>",
            ),
            AuthError::InvalidToken(msg) => (
                StatusCode::UNAUTHORIZED,
                // Don't leak internal details in production
                if cfg!(debug_assertions) {
                    return Response::builder()
                        .status(StatusCode::UNAUTHORIZED)
                        .header("Content-Type", "application/json")
                        .header("WWW-Authenticate", "Bearer realm=\"pmproxy\"")
                        .body(Body::from(format!(
                            r#"{{"error":"invalid_token","message":"{}"}}"#,
                            msg
                        )))
                        .unwrap();
                } else {
                    "Invalid authentication token"
                },
            ),
            AuthError::ExpiredToken => (
                StatusCode::UNAUTHORIZED,
                "Authentication token has expired",
            ),
            AuthError::RateLimited => (
                StatusCode::TOO_MANY_REQUESTS,
                "Rate limit exceeded. Please slow down.",
            ),
            AuthError::JwksFetchError(_) => (
                StatusCode::SERVICE_UNAVAILABLE,
                "Authentication service temporarily unavailable",
            ),
        };

        let body = format!(r#"{{"error":"{}","message":"{}"}}"#, error_code(&self), message);

        Response::builder()
            .status(status)
            .header("Content-Type", "application/json")
            .header(
                "WWW-Authenticate",
                match &self {
                    AuthError::RateLimited => "Bearer realm=\"pmproxy\", error=\"rate_limited\"",
                    AuthError::ExpiredToken => {
                        "Bearer realm=\"pmproxy\", error=\"invalid_token\", error_description=\"Token expired\""
                    }
                    _ => "Bearer realm=\"pmproxy\"",
                },
            )
            .body(Body::from(body))
            .unwrap()
    }
}

/// Get a machine-readable error code.
fn error_code(error: &AuthError) -> &'static str {
    match error {
        AuthError::MissingToken => "missing_token",
        AuthError::InvalidToken(_) => "invalid_token",
        AuthError::ExpiredToken => "expired_token",
        AuthError::RateLimited => "rate_limited",
        AuthError::JwksFetchError(_) => "service_unavailable",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::StatusCode;

    fn get_status(error: AuthError) -> StatusCode {
        let response = error.into_response();
        response.status()
    }

    #[test]
    fn test_error_status_codes() {
        assert_eq!(get_status(AuthError::MissingToken), StatusCode::UNAUTHORIZED);
        assert_eq!(
            get_status(AuthError::InvalidToken("test".to_string())),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(get_status(AuthError::ExpiredToken), StatusCode::UNAUTHORIZED);
        assert_eq!(
            get_status(AuthError::RateLimited),
            StatusCode::TOO_MANY_REQUESTS
        );
        assert_eq!(
            get_status(AuthError::JwksFetchError("test".to_string())),
            StatusCode::SERVICE_UNAVAILABLE
        );
    }
}
