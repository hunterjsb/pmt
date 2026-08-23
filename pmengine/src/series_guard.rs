//! Series partition guard — which market series THIS engine is allowed to trade.
//!
//! Two engines running under one operator account must never trade the same
//! series. Their orders sit on the same book under the same wallet, so a fire
//! from one can match a resting quote from the other: self-crossing, which is
//! wash-trade shaped no matter that neither side meant it. The partition is
//! declared per box and enforced here rather than by convention.
//!
//! `PMENGINE_SERIES_ALLOWLIST` is a comma-separated list of slug PREFIXES
//! (e.g. `xrp-updown-5m,sol-updown-5m`) — prefixes because a series' slugs
//! carry a per-window suffix (`xrp-updown-5m-1755990000`).
//!
//! UNSET (or empty) means no partition and every slug passes, so a box that
//! never sets it behaves byte-identically to one that predates this guard.

use std::env;

/// Env var naming this engine's partition. Unset = unpartitioned.
pub const SERIES_ALLOWLIST_VAR: &str = "PMENGINE_SERIES_ALLOWLIST";

/// A non-empty set of allowed slug prefixes.
///
/// Only ever constructed when the operator actually named something — an
/// empty allowlist would be a fail-closed engine that silently trades
/// nothing, which is worse than no partition at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SeriesAllowlist {
    prefixes: Vec<String>,
}

impl SeriesAllowlist {
    /// Read the partition from the environment. `None` when the var is unset,
    /// empty, or names nothing but separators.
    pub fn from_env() -> Option<Self> {
        Self::parse(env::var(SERIES_ALLOWLIST_VAR).ok().as_deref()?)
    }

    /// Parse a comma-separated prefix list. `None` when it names nothing.
    pub fn parse(raw: &str) -> Option<Self> {
        let prefixes: Vec<String> = raw
            .split(',')
            .map(|p| p.trim())
            .filter(|p| !p.is_empty())
            .map(str::to_string)
            .collect();
        (!prefixes.is_empty()).then_some(Self { prefixes })
    }

    /// The configured prefixes, for logging and refusal messages.
    pub fn prefixes(&self) -> &[String] {
        &self.prefixes
    }

    /// Whether `slug` falls inside this engine's partition.
    pub fn allows(&self, slug: &str) -> bool {
        self.prefixes.iter().any(|p| slug.starts_with(p.as_str()))
    }

    /// The refusal text. Names the allowlist so the operator can see at a
    /// glance whether the slug or the partition is the thing that's wrong.
    pub fn refusal(&self, slug: &str) -> String {
        format!(
            "series '{}' is outside this engine's {}=[{}] — refusing (a second engine \
             owns that series; two engines on one account crossing each other is \
             wash-trade shaped)",
            slug,
            SERIES_ALLOWLIST_VAR,
            self.prefixes.join(",")
        )
    }
}

/// `Ok(())` when `slug` may be traded under `allow`, `Err(refusal)` otherwise.
/// `None` is the unpartitioned case and always passes.
pub fn check(allow: Option<&SeriesAllowlist>, slug: &str) -> Result<(), String> {
    match allow {
        Some(a) if !a.allows(slug) => Err(a.refusal(slug)),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn allow(raw: &str) -> Option<SeriesAllowlist> {
        SeriesAllowlist::parse(raw)
    }

    #[test]
    fn an_unset_allowlist_passes_everything() {
        assert_eq!(check(None, "xrp-updown-5m-1755990000"), Ok(()));
        assert_eq!(check(None, "anything-at-all"), Ok(()));
        assert_eq!(check(None, ""), Ok(()));
    }

    #[test]
    fn blank_and_separator_only_values_are_no_partition_not_an_empty_one() {
        // A fail-closed engine that trades nothing is worse than an
        // unpartitioned one — refuse to read these as "allow nothing".
        assert_eq!(allow(""), None);
        assert_eq!(allow("   "), None);
        assert_eq!(allow(",,"), None);
        assert_eq!(allow(", ,"), None);
    }

    #[test]
    fn a_matching_prefix_passes_and_anything_else_is_refused() {
        let a = allow("xrp-updown-5m,sol-updown-5m").expect("parses");
        assert_eq!(check(Some(&a), "xrp-updown-5m-1755990000"), Ok(()));
        assert_eq!(check(Some(&a), "sol-updown-5m-1755990300"), Ok(()));

        let refused = check(Some(&a), "btc-updown-5m-1755990000").expect_err("outside partition");
        assert!(refused.contains("btc-updown-5m-1755990000"), "{refused}");
        assert!(refused.contains("xrp-updown-5m,sol-updown-5m"), "{refused}");
        assert!(refused.contains(SERIES_ALLOWLIST_VAR), "{refused}");
    }

    #[test]
    fn matching_is_a_prefix_not_an_equality_or_a_substring() {
        let a = allow("xrp-updown-5m").expect("parses");
        // The window suffix is the whole reason this is a prefix match.
        assert!(a.allows("xrp-updown-5m-1755990000"));
        assert!(a.allows("xrp-updown-5m"));
        // But a series that merely CONTAINS the prefix is a different series.
        assert!(!a.allows("wrapped-xrp-updown-5m-1755990000"));
        // And a longer-cadence sibling is not the 5m series.
        assert!(!a.allows("xrp-updown-1h-1755990000"));
    }

    #[test]
    fn whitespace_around_entries_is_tolerated() {
        // Operators paste these into unit files; a stray space must not
        // silently create a prefix nothing can ever match.
        let a = allow(" xrp-updown-5m , sol-updown-5m ").expect("parses");
        assert_eq!(a.prefixes(), ["xrp-updown-5m", "sol-updown-5m"]);
        assert!(a.allows("xrp-updown-5m-1755990000"));
    }

    #[test]
    fn a_single_entry_partitions_just_as_hard() {
        let a = allow("xrp-updown-5m").expect("parses");
        assert!(a.allows("xrp-updown-5m-1"));
        assert!(!a.allows("sol-updown-5m-1"));
    }
}
