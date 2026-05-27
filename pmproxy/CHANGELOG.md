# Changelog

All notable pmproxy changes are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from 1.0.0 onward; 0.x releases moved fast and may have broken between
minors.

## [Unreleased]

### Added
- `pmproxy/RUNBOOK.md` — operational reference (health check, common
  problems, rollback procedure).
- `cargo audit` step in CI (`.github/workflows/ci.yml`) — fails build on
  any RUSTSEC advisory.
- `src/upstream.rs` — single `route()` function replaces the 6× repeated
  prefix-match pattern in `proxy_handler`. 5 dedicated tests.
- `src/headers.rs` — request/response forwarding policy with the POLY_*
  canonical-name table and RFC 7230 hop-by-hop allowlist factored out.

### Changed
- `proxy_handler` slimmed by 81 lines; now linear and obvious.
- `CognitoClaims` dropped unused `username` and `client_id` fields.
- `TenantRateLimiter` dropped unused `config` field and dead
  `cleanup_stale` method.

### Fixed
- Startup log line claimed `/chain/* → polygon-rpc.com` but the actual
  upstream has been `polygon-bor-rpc.publicnode.com` since 0.5.0.
- 7 transitive-dep RUSTSEC advisories closed via `cargo update`
  (bytes, quinn-proto, rustls-webpki ×3, time, rand).

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
