# Security Policy

## Reporting a Vulnerability

Please email **hunterjsb@gmail.com** with the subject line `SECURITY: pmt`.
Do **not** open a public GitHub issue for vulnerabilities. Encrypt sensitive
report content if possible.

You can expect:
- Acknowledgement within 72 hours.
- An initial assessment within 7 days.
- A fix-or-mitigation plan communicated back to you before any public
  disclosure.

## Scope

The pmt monorepo ships three deployable components. Different threat models
apply to each:

### `pmproxy` (Lambda + EC2 binary, public-facing)

In scope:
- JWT validation bypass or forgery against the `/clob`, `/gamma`, `/chain`,
  or `/clob/ws/{channel}` routes.
- Tenant-rate-limit bypass (per-tenant or system-wide).
- Header smuggling that lets a client forge Polymarket auth (e.g. injecting
  `POLY_*` headers that bypass our forwarding policy).
- JWKS-cache poisoning or amplification attacks.
- Request body / response body injection.
- DoS attacks that don't require holding a valid JWT (cost-amplification).

Out of scope for now (documented gaps, not vulnerabilities):
- The `/chain/*` route forwards arbitrary JSON-RPC bodies to the upstream
  Polygon RPC. Method allowlisting is not implemented; the route assumes
  the holder of a valid JWT is trusted (today: single-tenant).
- WebSocket connections are rate-limited at upgrade time only; frames sent
  after a successful upgrade are not metered.

### `pmtrader` (Python CLI + SDK)

In scope:
- Credential leakage from `PolymarketAPI` (private key, Cognito credentials,
  cached CLOB API creds at `~/.cache/pmt/`).
- Order-flow vulnerabilities (price/size manipulation through the CLI's
  argument parsing, particularly `pmt buy/sell`).

Out of scope:
- Use of a hot wallet private key in `.env` is a known operational pattern;
  rotate frequently.

### `pmengine` (Rust HFT engine, local-only)

The control plane binds `127.0.0.1:7531` by default and is not intended to
be exposed beyond localhost. Reports against the engine assume it is
listening on a routable interface.

## Supported Versions

| Component  | Supported version |
| ---------- | ----------------- |
| pmproxy    | latest tagged release on master (`pmproxy-v*`) |
| pmtrader   | latest tagged release on master (`pmtrader-v*`) |
| pmengine   | latest tagged release on master (`pmengine-v*`) |

Older versions are not patched. Subscribe to GitHub releases for upgrade
notifications.
