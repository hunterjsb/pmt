# pmproxy

Dead-simple HTTP reverse proxy for Polymarket APIs. Deploy close to Polymarket servers (or in NL) to bypass VPN restrictions and reduce latency.

## What It Does

Forwards HTTP requests to Polymarket APIs transparently. Your Python client handles auth - the proxy just forwards everything.

```
Python client → pmproxy → Polymarket APIs
```

## Build & Run

```bash
cargo build --release
./target/release/pmproxy
```

Runs on `http://0.0.0.0:8080` by default.

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

**Docker**:
```bash
docker build -t pmproxy .
docker run -p 8080:8080 pmproxy
```

**fly.io** (Amsterdam near you in NL):
```bash
fly launch --name pmproxy --region ams
fly deploy
```

**Near Polymarket** (US-East for lowest order latency):
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

**155 lines of Rust** - just routes requests and forwards responses. No parsing, no auth logic, no complexity.

Your Python client does all the auth signing. The proxy just passes headers/body through unchanged.

## Testing

```bash
# Public endpoints
curl http://localhost:8080/gamma/events?limit=5
curl http://localhost:8080/clob/sampling-markets

# Authenticated (use Python client, it adds auth headers)
python -c "from polymarket.clob import create_authenticated_clob; c=create_authenticated_clob(); print(len(c.trades()))"
```
