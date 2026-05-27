//! Path → upstream URL mapping.
//!
//! Single source of truth for which incoming path prefix lands at which
//! external service. Keep this in sync with the README routes table.

const CLOB_BASE: &str = "https://clob.polymarket.com";
const GAMMA_BASE: &str = "https://gamma-api.polymarket.com";
const CHAIN_BASE: &str = "https://polygon-bor-rpc.publicnode.com";

/// Result of routing a request path. None means no known prefix matched.
pub struct Route {
    pub upstream_base: &'static str,
    pub upstream_path: String,
    /// A short identifier for metrics/logging (e.g. "clob", "gamma", "chain").
    pub label: &'static str,
}

/// Match `path` against a route prefix and build the upstream URL fragment.
///
/// Strips the route prefix (with or without trailing slash) and returns the
/// remaining path plus the upstream base URL. `/chain` (no path) collapses
/// to root, so the JSON-RPC endpoint can be hit at `/chain/`.
///
/// Returns None if the path contains any `..` segment. Polymarket's upstream
/// servers normalize path-traversal sequences server-side and may route
/// cross-host (e.g. `clob.polymarket.com/../gamma` → Gamma), so we reject
/// `..` before forwarding as defense in depth. Surfaced by deep e2e tests.
pub fn route(path: &str) -> Option<Route> {
    if path.split('/').any(|seg| seg == "..") {
        return None;
    }
    for (prefix, base, label) in [
        ("/clob", CLOB_BASE, "clob"),
        ("/gamma", GAMMA_BASE, "gamma"),
        ("/chain", CHAIN_BASE, "chain"),
    ] {
        if path == prefix {
            return Some(Route { upstream_base: base, upstream_path: String::new(), label });
        }
        if let Some(rest) = path.strip_prefix(&format!("{}/", prefix)) {
            return Some(Route { upstream_base: base, upstream_path: rest.to_string(), label });
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn route_clob_with_path() {
        let r = route("/clob/sampling-markets").unwrap();
        assert_eq!(r.upstream_base, CLOB_BASE);
        assert_eq!(r.upstream_path, "sampling-markets");
        assert_eq!(r.label, "clob");
    }

    #[test]
    fn route_gamma_bare() {
        let r = route("/gamma").unwrap();
        assert_eq!(r.upstream_base, GAMMA_BASE);
        assert_eq!(r.upstream_path, "");
        assert_eq!(r.label, "gamma");
    }

    #[test]
    fn route_chain_root() {
        let r = route("/chain/").unwrap();
        assert_eq!(r.upstream_base, CHAIN_BASE);
        assert_eq!(r.upstream_path, "");
        assert_eq!(r.label, "chain");
    }

    #[test]
    fn route_unknown() {
        assert!(route("/nonsense").is_none());
        assert!(route("/").is_none());
        assert!(route("").is_none());
    }

    #[test]
    fn route_no_prefix_leak() {
        // /clobbermuns shouldn't match /clob*
        assert!(route("/clobbermuns").is_none());
    }

    #[test]
    fn route_rejects_dotdot() {
        // Path traversal — even though our route() faithfully strips the
        // prefix, Polymarket's upstream gateway can normalize `..` and
        // serve content from a different host. We reject before forwarding.
        assert!(route("/clob/../gamma").is_none());
        assert!(route("/clob/foo/../bar").is_none());
        assert!(route("/chain/..").is_none());
        // Legitimate paths with `.` aren't blocked
        assert!(route("/clob/markets/0x.deadbeef").is_some());
        // Dot in segment-but-not-segment is fine ("..foo" is not a `..` segment)
        assert!(route("/clob/..foo").is_some());
    }
}
