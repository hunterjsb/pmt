# Changelog

All notable pmproxy changes are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from 1.0.0 onward; 0.x releases moved fast and may have broken between
minors.

## [1.0.1] — 2026-05-27

### Defense in depth
- `upstream::route` now returns None for any path containing a `..`
  segment. Surfaced during deep e2e testing: `/clob/../gamma` was
  returning Gamma's homepage with a 200. Investigation showed Lambda
  Function URL (and most HTTP clients) normalize `..` BEFORE our
  handler sees the request — so on Lambda, the path arrives at
  pmproxy already resolved to `/gamma` and our `..` check never
  triggers. The behavior is correct (legitimate gamma route), just
  initially surprising.

  The fix is still valuable: a future EC2 deployment may not benefit
  from the same upstream path normalization, and rejecting `..` at
  our boundary is cheap belt-and-suspenders. `route_rejects_dotdot`
  unit test verifies the function-level rejection works.

### Added
- `pmproxy/tests/test_deep.py` — heavyweight verification suite (22
  tests) covering end-to-end JWT failures, latency percentiles, metric
  counter accuracy, concurrency-within-burst, failure injection, and
  an intentional rate-limit-tripping burst. Run manually post-deploy.
- `pmproxy/RUNBOOK.md` deep-testing section with reproducible recipes
  for the deep suite, manual WS bridge verification, and `/chain`
  allowlist verification.

## [1.0.0] — 2026-05-27

First stable release. The 1.x route surface (`/clob/*`, `/gamma/*`,
`/chain/*`, `/clob/ws/{channel}`, `/health`, `/badge`, `/metrics`) and
the JWT claim model (`sub`, `exp`, `iss`, `token_use`, optional
`custom:tenant_tier`) are frozen — breaking changes require a 2.0.

### Added — observability & ops
- `/metrics` Prometheus-text endpoint exposing per-route request
  counts, auth failure breakdown, rate-limit drops, JWKS refresh
  outcomes, WS connection counts, and per-tenant gauge.
- `pmproxy/RUNBOOK.md` — health check, common-problem triage, and
  step-by-step rollback procedure.
- `SECURITY.md` at repo root — report channel, per-component scope,
  supported-versions policy.
- `pmproxy/CHANGELOG.md` (this file) — keep-a-changelog format,
  backfilled from tags.
- `cargo audit` step in CI — fails build on any RUSTSEC advisory.
- SNS topic + 4 new CloudWatch alarms (5xx, function-errors,
  throttles, p99 latency) via Pulumi. Recovery announced too.

### Added — security
- Optional `/chain/*` JSON-RPC method allowlist
  (`PMPROXY_CHAIN_METHOD_ALLOWLIST`). Default pass-through preserved for
  single-tenant deployments. Documented in `SECURITY.md`.
- Per-WS-session client→upstream frame rate limit (10/s sustained,
  50-frame burst). Frames over budget are dropped without tearing
  down the session.
- JWT failure-path tests: expired, wrong issuer, wrong audience,
  missing kid, unknown kid, wrong token_use, missing required claim,
  signature mismatch (10 new tests).
- WS round-trip integration test against a mock upstream.
- Deployed-Lambda smoke test moved from `pmtrader/tests/test_proxy.py`
  to `pmproxy/tests/test_deployed.py` — standalone, uses boto3
  directly, no cross-package coupling.

### Added — internal structure
- `src/upstream.rs` — single `route()` replaces 6 repeated prefix-match
  branches in `proxy_handler`.
- `src/headers.rs` — request/response forwarding policy with the
  POLY_* canonical-name table and RFC 7230 hop-by-hop allowlist.
- `src/chain.rs` — JSON-RPC method validation for the allowlist.
- `src/metrics.rs` — atomic counters + Prometheus text rendering.
- `PMPROXY_WS_UPSTREAM_BASE` env var to redirect WS upstream (used by
  integration tests).

### Changed
- `proxy_handler` slimmed by ~80 lines; now linear and obvious.
- `CognitoClaims` dropped unused `username` and `client_id` fields.
- `TenantRateLimiter` dropped unused `config` field and dead
  `cleanup_stale` method.
- `place_buy` / `place_sell` on `PolymarketAPI` collapsed into
  `place(side, ...)` (pmtrader-side, but mentioned here because the
  proxy contract is unaffected — same wire format).

### Fixed
- Startup log line claimed `/chain/* → polygon-rpc.com` but the actual
  upstream has been `polygon-bor-rpc.publicnode.com` since 0.5.0.
- 7 transitive-dep RUSTSEC advisories closed via `cargo update`
  (bytes, quinn-proto, rustls-webpki ×3, time, rand).

### Test coverage
- 18 → 36 in-crate unit tests
- 2 new feature-gated integration tests (WS round-trip + unknown-channel
  rejection)
- 8 post-deploy smoke tests against the live Lambda

## [0.5.0] — 2026-05-27

### Added
- `/clob/ws/{channel}` — WebSocket bridge to
  `wss://ws-subscriptions-clob.polymarket.com/ws/{channel}`. EC2-only
  (gated behind the `ws` feature; Lambda Function URLs can't upgrade).
  Only `market` and `user` channels accepted.
- `/badge` — shields.io JSON schema for the README live-status badge.

### Changed
- `/chain/*` now forwards to `polygon-bor-rpc.publicnode.com` (was
  `polygon-rpc.com`, which became unreliable).
- Pruned unused dependencies; smaller binary.
- Removed dead CodeDeploy scaffolding (deploy went all-Lambda earlier).

## [0.4.0] — 2026-01-18

### Added
- Multi-tenant Cognito JWT authentication (`PMPROXY_AUTH_ENABLED=true`):
  validates incoming Bearer tokens against the configured Cognito User
  Pool's JWKS, with a 1-hour cache.
- Per-tenant rate limiting based on a `custom:tenant_tier` claim
  (free / pro / enterprise → 60 / 300 / 1000 rpm).

## [0.3.6] — 2026-01-04

- Pre-0.4 patch series. See `git log pmproxy-v0.3.0..pmproxy-v0.3.6`.

## [0.1.0] — 2026-01

- Initial release: HTTP reverse proxy with `/clob/*`, `/gamma/*`,
  `/chain/*` routes. No auth, no rate limiting.
