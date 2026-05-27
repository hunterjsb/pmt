//! Header forwarding policy.
//!
//! `Host` and `Authorization` are stripped on the way upstream — `Host`
//! because reqwest sets it from the URL, and `Authorization` because that
//! header is OUR Cognito JWT, not upstream credentials.
//!
//! Polymarket's `POLY_*` auth headers arrive lowercase from axum's
//! lowercase HeaderMap; we restore canonical casing so upstream signatures
//! validate. Hop-by-hop response headers are stripped per RFC 7230 §6.1.

use axum::http::HeaderMap;
use reqwest::RequestBuilder;

/// Headers we drop entirely when forwarding upstream.
fn skip_request_header(name: &str) -> bool {
    matches!(name, "host" | "authorization")
}

/// Hop-by-hop headers per RFC 7230 §6.1 — never forwarded back to the client.
fn skip_response_header(name: &str) -> bool {
    matches!(
        name,
        "connection"
            | "transfer-encoding"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "trailer"
            | "upgrade",
    )
}

/// Polymarket-specific request headers that must keep upper-case naming.
fn canonical_request_name(lower: &str) -> &str {
    match lower {
        "poly_address" => "POLY_ADDRESS",
        "poly_signature" => "POLY_SIGNATURE",
        "poly_timestamp" => "POLY_TIMESTAMP",
        "poly_nonce" => "POLY_NONCE",
        "poly_api_key" => "POLY_API_KEY",
        "poly_passphrase" => "POLY_PASSPHRASE",
        other => other,
    }
}

/// Attach all client headers (with our forwarding policy) to an outgoing request.
pub fn forward_request_headers(mut req: RequestBuilder, headers: &HeaderMap) -> RequestBuilder {
    for (name, value) in headers.iter() {
        let lower = name.as_str();
        if skip_request_header(lower) {
            continue;
        }
        req = req.header(canonical_request_name(lower), value);
    }
    req
}

/// Copy upstream response headers onto the outgoing response, minus hop-by-hop.
pub fn forward_response_headers(
    mut builder: axum::http::response::Builder,
    headers: &reqwest::header::HeaderMap,
) -> axum::http::response::Builder {
    for (name, value) in headers.iter() {
        if !skip_response_header(name.as_str()) {
            builder = builder.header(name, value);
        }
    }
    builder
}
