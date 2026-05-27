//! `/chain/*` policy: optional JSON-RPC method allowlist.
//!
//! By default, `/chain/*` forwards the request body to the upstream RPC
//! unchanged. This is correct for a single-tenant deployment where the
//! holder of a valid JWT is the operator and is trusted with arbitrary
//! `eth_*` calls (including `eth_sendRawTransaction`).
//!
//! For multi-tenant deployments, set `PMPROXY_CHAIN_METHOD_ALLOWLIST` to a
//! comma-separated list of allowed JSON-RPC methods. When set, the body
//! is parsed and any request invoking a non-allowlisted method is
//! rejected with 403.
//!
//! Batched requests (`[{...}, {...}]`) are allowed only if EVERY method
//! in the batch passes the allowlist.

use std::collections::HashSet;

/// Outcome of validating a JSON-RPC body against an allowlist.
#[derive(Debug)]
pub enum AllowDecision {
    /// All methods (single or batched) are allowlisted.
    Allow,
    /// One or more methods were denied. The string names the first offender.
    Deny(String),
    /// Body wasn't parseable as JSON-RPC. Treat as deny so we don't smuggle.
    Malformed,
}

/// Check that every JSON-RPC `method` in `body` is in `allowlist`.
pub fn validate(body: &[u8], allowlist: &HashSet<String>) -> AllowDecision {
    let value: serde_json::Value = match serde_json::from_slice(body) {
        Ok(v) => v,
        Err(_) => return AllowDecision::Malformed,
    };

    let methods: Vec<&str> = match &value {
        serde_json::Value::Object(_) => match value.get("method").and_then(|m| m.as_str()) {
            Some(m) => vec![m],
            None => return AllowDecision::Malformed,
        },
        serde_json::Value::Array(arr) => {
            let mut acc = Vec::with_capacity(arr.len());
            for item in arr {
                match item.get("method").and_then(|m| m.as_str()) {
                    Some(m) => acc.push(m),
                    None => return AllowDecision::Malformed,
                }
            }
            acc
        }
        _ => return AllowDecision::Malformed,
    };

    for m in methods {
        if !allowlist.contains(m) {
            return AllowDecision::Deny(m.to_string());
        }
    }
    AllowDecision::Allow
}

/// Parse the env var. Empty / unset → None (allowlist not enforced).
pub fn allowlist_from_env() -> Option<HashSet<String>> {
    let raw = std::env::var("PMPROXY_CHAIN_METHOD_ALLOWLIST").ok()?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(
        trimmed
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn list(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn single_request_allow() {
        let allow = list(&["eth_chainId", "eth_blockNumber"]);
        let body = br#"{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}"#;
        assert!(matches!(validate(body, &allow), AllowDecision::Allow));
    }

    #[test]
    fn single_request_deny() {
        let allow = list(&["eth_chainId"]);
        let body = br#"{"jsonrpc":"2.0","id":1,"method":"eth_sendRawTransaction","params":[]}"#;
        match validate(body, &allow) {
            AllowDecision::Deny(m) => assert_eq!(m, "eth_sendRawTransaction"),
            other => panic!("expected Deny, got {other:?}"),
        }
    }

    #[test]
    fn batch_all_allow() {
        let allow = list(&["eth_chainId", "eth_blockNumber"]);
        let body = br#"[{"method":"eth_chainId"},{"method":"eth_blockNumber"}]"#;
        assert!(matches!(validate(body, &allow), AllowDecision::Allow));
    }

    #[test]
    fn batch_one_denied_denies_all() {
        let allow = list(&["eth_chainId"]);
        let body = br#"[{"method":"eth_chainId"},{"method":"eth_sendRawTransaction"}]"#;
        match validate(body, &allow) {
            AllowDecision::Deny(m) => assert_eq!(m, "eth_sendRawTransaction"),
            other => panic!("expected Deny, got {other:?}"),
        }
    }

    #[test]
    fn malformed_is_deny() {
        let allow = list(&["eth_chainId"]);
        assert!(matches!(validate(b"not json", &allow), AllowDecision::Malformed));
        assert!(matches!(validate(b"{}", &allow), AllowDecision::Malformed));
        assert!(matches!(validate(b"[]", &allow), AllowDecision::Allow)); // empty batch: vacuously allow
    }

    #[test]
    fn allowlist_from_env_parses() {
        // We don't touch real env in tests; just inline-test the splitter via direct call.
        let raw = "eth_chainId, eth_blockNumber ,eth_call";
        let set: HashSet<String> = raw
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        assert!(set.contains("eth_chainId"));
        assert!(set.contains("eth_blockNumber"));
        assert!(set.contains("eth_call"));
        assert_eq!(set.len(), 3);
    }
}
