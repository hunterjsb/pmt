# pmproxy

Dead-simple HTTP reverse proxy for Polymarket APIs. Deploy close to Polymarket servers (or in NL) to bypass VPN restrictions and reduce latency.

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

See [Deploy](#deploy) section for full Lambda setup.

## Routes

- `/clob/*` → `https://clob.polymarket.com/*`
- `/gamma/*` → `https://gamma-api.polymarket.com/*`
- `/chain/*` → `https://polygon-rpc.com`

## Usage from Python

```python
from py_clob_client.client import ClobClient

# Point client at proxy instead of direct API
client = ClobClient(
    'http://localhost:8080/clob',  # or http://your-nl-server:8080/clob
    key=os.environ['PM_PRIVATE_KEY'],
    chain_id=137,
    signature_type=0,
    funder=os.environ['PM_FUNDER_ADDRESS'],
)

# Everything works exactly the same
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

trades = client.get_trades()  # Proxied!
orders = client.get_orders()  # Proxied!
```

Or just change the base URL in your existing code:

```python
import requests

# Before: requests.get("https://gamma-api.polymarket.com/events")
# After:
requests.get("http://localhost:8080/gamma/events")
```

## Deploy

### EC2 (eu-west-1 for performance)

```bash
# Build for Linux
cargo build --release --features ec2 --target x86_64-unknown-linux-gnu

# On EC2
./pmproxy -p 8080
```

### Lambda (cost-effective, no performance requirements)

Install [cargo-lambda](https://www.cargo-lambda.info/):
```bash
brew tap cargo-lambda/cargo-lambda
brew install cargo-lambda
```

Build and deploy:
```bash
# Build for Lambda runtime
cargo lambda build --release --features lambda --bin pmproxy-lambda

# Deploy to eu-west-1 with Function URL
cargo lambda deploy pmproxy-lambda \
  --region eu-west-1 \
  --enable-function-url

# Or with API Gateway (more control over routing)
# Use AWS SAM/CDK/Pulumi for production
```

Lambda considerations:
- ~50-100ms cold start (Rust is fast)
- Pay per request, not per hour
- Function URL gives you `https://<id>.lambda-url.eu-west-1.on.aws`
- Good for low-volume or bursty traffic

### Docker

```bash
docker build -t pmproxy .
docker run -p 8080:8080 pmproxy
```

### fly.io

Amsterdam (near NL):
```bash
fly launch --name pmproxy --region ams
fly deploy
```

Near Polymarket (US-East for lowest order latency):
```bash
fly launch --name pmproxy --region iad
```

## CLI Options

```
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

Your Python client does all the auth signing. The proxy just passes headers/body through unchanged.

## Testing

```bash
# Public endpoints
curl http://localhost:8080/gamma/events?limit=5
curl http://localhost:8080/clob/sampling-markets

# Authenticated (use Python client, it adds auth headers)
python -c "from polymarket.clob import create_authenticated_clob; c=create_authenticated_clob(); print(len(c.trades()))"
```
