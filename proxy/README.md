# pmproxy

Dead-simple HTTP reverse proxy for Polymarket APIs. Deploy close to Polymarket servers for performance, rate control, and cost-effectiveness.

## What It Does

Forwards HTTP requests to Polymarket APIs transparently. Your Python client handles auth - the proxy just forwards everything.

```
Python client → pmproxy → Polymarket APIs
```

## Build & Run

**EC2/Server (for performance)**:
```bash
cargo build --release --features ec2
./target/release/pmproxy
```

Runs on `http://0.0.0.0:8080` by default.

**Lambda (for cost-effective proxy)**:
```bash
# Cross-compile for Lambda (Amazon Linux 2023)
cargo lambda build --release --features lambda --bin pmproxy-lambda

# Deploy
cargo lambda deploy pmproxy-lambda
```

## Routes

- `/clob/*` → `https://clob.polymarket.com/*`
- `/gamma/*` → `https://gamma-api.polymarket.com/*`
- `/chain/*` → `https://polygon-rpc.com`

## CLI Options

```bash
pmproxy [OPTIONS]

Options:
  -H, --host <HOST>       Host to bind [default: 0.0.0.0]
  -p, --port <PORT>       Port [default: 8080]
  -l, --log-level <LEVEL> Log level [default: info]
```

## Architecture

Simple Rust proxy - routes requests and forwards responses. No parsing, no auth logic, no complexity.

```
src/
├── lib.rs      # Core proxy logic (shared)
├── main.rs     # EC2 server binary (tokio)
└── lambda.rs   # Lambda handler binary
```

`pmproxy` has a Python client does all the auth signing. The proxy just passes headers/body through unchanged.

## Testing

```bash
# Public endpoints
curl http://localhost:8080/gamma/events?limit=5
curl http://localhost:8080/clob/sampling-markets
```
