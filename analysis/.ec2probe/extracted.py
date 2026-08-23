#!/usr/bin/env python3
"""net_probe.py — what the wire actually costs from THIS box, stage by stage.

Issue #4 phase 7/13 asks us not to guess where latency is. The tape can only
show us decision-to-fill; everything between "we decided" and "the exchange
saw it" is network, and none of it is recorded. This probes it directly and
splits every round trip into the four stages that behave differently:

    DNS      getaddrinfo (cached vs cold is night and day)
    TCP      SYN -> ACK, the pure one-way-times-two floor to the endpoint
    TLS      handshake on top of an established socket (1-RTT resume vs 2-RTT full)
    HTTP     request write -> first byte of response, on a WARM connection

The last one is the number that matters for an order: pmengine keeps a
connection pool, so a steady-state order pays HTTP-on-warm-socket, not the
DNS+TCP+TLS setup. Both are reported; conflating them is how people talk
themselves into a 300ms order path that is really 40ms.

Websocket endpoints are probed for handshake time only (TCP+TLS+HTTP 101).
We do not subscribe, we do not stream, we disconnect immediately.

Gentle by construction: one sample per endpoint per pass, passes spaced by
--gap seconds, default 20 passes. That is ~20 requests per endpoint over
several minutes, which is less traffic than the engine's own book poller
generates in the same period.

    cd pmtrader && .venv/bin/python ../analysis/net_probe.py
    cd pmtrader && .venv/bin/python ../analysis/net_probe.py -n 20 --gap 3 \
        --out ../analysis/net_probe_raw.json

Read-only. Touches no trading state, places no orders, sends no auth.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import statistics
import sys
import time
from collections import defaultdict
from urllib.parse import urlsplit

# ---------------------------------------------------------------- endpoints
#
# Each entry: (label, url, kind). "https" does a full GET; "wss" does the
# upgrade handshake and hangs up on the 101.
#
# The CLOB /book GET needs a real token id or it 400s -- a 400 is still a
# complete round trip so the timing is valid either way, but a 200 exercises
# the same handler an order does. This token is the BTC 5m "Up" leg from a
# long-settled window: public, harmless, and it will keep answering.
SAMPLE_TOKEN = (
    "80154749963156837049567771341272927057607215770019852042685337393456182362003"
)

ENDPOINTS = [
    # The order path itself. This is the one the verdict hangs on.
    ("clob.book", f"https://clob.polymarket.com/book?token_id={SAMPLE_TOKEN}", "https"),
    ("clob.time", "https://clob.polymarket.com/time", "https"),
    ("gamma", "https://gamma-api.polymarket.com/markets?limit=1", "https"),
    ("data-api", "https://data-api.polymarket.com/activity?user=0x0000000000000000000000000000000000000000&limit=1", "https"),
    ("binance.vision", "https://data-api.binance.vision/api/v3/time", "https"),
    ("binance.api", "https://api.binance.com/api/v3/time", "https"),
    # Streams: handshake only.
    ("ws.binance", "wss://stream.binance.com:9443/ws/btcusdt@trade", "wss"),
    ("ws.binance.vis", "wss://data-stream.binance.vision/ws/btcusdt@trade", "wss"),
    ("ws.pm-rtds", "wss://ws-live-data.polymarket.com/", "wss"),
    ("ws.clob-mkt", "wss://ws-subscriptions-clob.polymarket.com/ws/market", "wss"),
]

# PMPROXY_URL, if the engine routes orders through the Lambda, is part of the
# order path and has to be measured too. Added at runtime so the script still
# runs on a box with no .env.
PROXY_ENV = "PMPROXY_URL"

UA = "pmtrader-netprobe/1.0"


# ---------------------------------------------------------------- probing


def _resolve(host: str, port: int) -> tuple[float, tuple]:
    t0 = time.perf_counter()
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    return (time.perf_counter() - t0) * 1000.0, infos[0][4]


def _connect(addr: tuple, timeout: float) -> tuple[float, socket.socket]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    t0 = time.perf_counter()
    s.connect(addr)
    return (time.perf_counter() - t0) * 1000.0, s


def _wrap_tls(s: socket.socket, host: str, ctx: ssl.SSLContext) -> tuple[float, ssl.SSLSocket]:
    t0 = time.perf_counter()
    ss = ctx.wrap_socket(s, server_hostname=host)
    return (time.perf_counter() - t0) * 1000.0, ss


def _http_once(ss: ssl.SSLSocket, host: str, path: str) -> tuple[float, float, int]:
    """(time to first byte, time to full response, status) on an already-open socket.

    The body MUST be fully drained before returning: leaving bytes in the
    receive buffer makes the next request on the same socket appear to
    answer in ~0ms (it is reading the previous response). That artefact is
    exactly the kind of thing that would flatter a warm-RTT number, so the
    length/chunked parsing below is load-bearing, not tidiness.
    """
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {UA}\r\n"
        f"Accept: */*\r\nAccept-Encoding: identity\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode()
    t0 = time.perf_counter()
    ss.sendall(req)
    buf = ss.recv(65536)
    ttfb = (time.perf_counter() - t0) * 1000.0
    while b"\r\n\r\n" not in buf:
        chunk = ss.recv(65536)
        if not chunk:
            break
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    status = 0
    try:
        status = int(head.split(b"\r\n", 1)[0].split()[1])
    except Exception:
        pass
    hl = {}
    for ln in head.split(b"\r\n")[1:]:
        k, _, v = ln.partition(b":")
        hl[k.strip().lower()] = v.strip()
    if b"chunked" in hl.get(b"transfer-encoding", b""):
        while not body.endswith(b"0\r\n\r\n"):
            chunk = ss.recv(65536)
            if not chunk:
                break
            body += chunk
    else:
        want = int(hl.get(b"content-length", b"0") or 0)
        while len(body) < want:
            chunk = ss.recv(65536)
            if not chunk:
                break
            body += chunk
    done = (time.perf_counter() - t0) * 1000.0
    return ttfb, done, status


def probe_https(url: str, timeout: float, warm_reqs: int = 3) -> dict:
    """One cold setup + `warm_reqs` requests reusing the connection.

    The warm numbers are the honest model of an order on a pooled client;
    the cold ones are what a reconnect costs.
    """
    u = urlsplit(url)
    host = u.hostname
    port = u.port or 443
    path = u.path + (("?" + u.query) if u.query else "")
    out: dict = {"host": host}
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    try:
        out["dns_ms"], addr = _resolve(host, port)
        out["ip"] = addr[0]
        out["tcp_ms"], s = _connect(addr, timeout)
        try:
            out["tls_ms"], ss = _wrap_tls(s, host, ctx)
            out["tls_version"] = ss.version()
            ttfb, done, status = _http_once(ss, host, path)
            out["http_cold_ms"] = ttfb
            out["status"] = status
            warm = []
            for _ in range(warm_reqs):
                try:
                    w, _d, _st = _http_once(ss, host, path)
                    warm.append(w)
                except Exception:
                    break
            if warm:
                out["http_warm_ms"] = min(warm)
                out["http_warm_all"] = warm
            ss.close()
        finally:
            try:
                s.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001 - a failed probe is data, not a crash
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def probe_wss(url: str, timeout: float) -> dict:
    """TCP + TLS + the HTTP/1.1 Upgrade round trip, up to the 101."""
    u = urlsplit(url)
    host = u.hostname
    port = u.port or 443
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    out: dict = {"host": host}
    ctx = ssl.create_default_context()
    try:
        out["dns_ms"], addr = _resolve(host, port)
        out["ip"] = addr[0]
        out["tcp_ms"], s = _connect(addr, timeout)
        try:
            out["tls_ms"], ss = _wrap_tls(s, host, ctx)
            key = base64.b64encode(os.urandom(16)).decode()
            req = (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n\r\n"
            ).encode()
            t0 = time.perf_counter()
            ss.sendall(req)
            resp = ss.recv(4096)
            out["upgrade_ms"] = (time.perf_counter() - t0) * 1000.0
            try:
                out["status"] = int(resp.split(b"\r\n", 1)[0].split()[1])
            except Exception:
                out["status"] = 0
            ss.close()
        finally:
            try:
                s.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---------------------------------------------------------------- reporting


def q(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[i]


def summarize(samples: list[dict], key: str) -> str:
    xs = [s[key] for s in samples if key in s and isinstance(s[key], (int, float))]
    if not xs:
        return f"{'-':>7} {'-':>7} {'-':>7} {'-':>7} {'-':>7}"
    return (
        f"{min(xs):7.1f} {q(xs,0.5):7.1f} {q(xs,0.9):7.1f} {max(xs):7.1f} "
        f"{(statistics.pstdev(xs) if len(xs) > 1 else 0.0):7.1f}"
    )


def probe_order_path_ab(n: int, gap: float, env_path: str) -> None:
    """A/B the ACTUAL order path: direct to clob vs through the eu-west-1 Lambda.

    pmengine's `l2_post` sends POST /order to `{PMPROXY_URL}/clob/order` when
    PMPROXY_URL is set, and straight to clob.polymarket.com when it is not
    (pmengine/src/client.rs:243-247). Everything else about the request is
    identical, so the only way to price that routing choice is to send the
    same harmless GET both ways and difference them.

    GET /time is used because it is trivial server-side: whatever the gap is,
    it is transport, not query cost. Read-only, no order is placed, and the
    proxy call is SigV4-signed exactly the way the engine signs it.
    """
    proxy = os.environ.get(PROXY_ENV, "")
    if not proxy and os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line.startswith(PROXY_ENV + "="):
                proxy = line.split("=", 1)[1].strip().strip('"')
            if line.startswith("PMPROXY_AWS_REGION="):
                os.environ.setdefault("PMPROXY_AWS_REGION",
                                      line.split("=", 1)[1].strip().strip('"'))
    if not proxy:
        print("\n(no PMPROXY_URL — skipping the direct-vs-proxy A/B)")
        return
    try:
        import hashlib  # noqa: PLC0415

        import boto3  # noqa: PLC0415
        import requests  # noqa: PLC0415
        from botocore.auth import SigV4Auth  # noqa: PLC0415
        from botocore.awsrequest import AWSRequest  # noqa: PLC0415
    except ImportError as e:
        print(f"\n(A/B skipped: {e})")
        return
    creds = boto3.Session().get_credentials()
    if creds is None:
        print("\n(A/B skipped: no AWS credentials to sign the proxy call)")
        return
    region = os.environ.get("PMPROXY_AWS_REGION", "eu-west-1")

    direct_url = "https://clob.polymarket.com/time"
    proxy_url = proxy.rstrip("/") + "/clob/time"
    s_direct, s_proxy = requests.Session(), requests.Session()
    d, p, dst, pst = [], [], set(), set()
    for i in range(n):
        try:
            t0 = time.perf_counter()
            r = s_direct.get(direct_url, headers={"User-Agent": UA}, timeout=10)
            d.append((time.perf_counter() - t0) * 1000.0)
            dst.add(r.status_code)
        except Exception as e:  # noqa: BLE001
            dst.add(str(e)[:40])
        try:
            req = AWSRequest(method="GET", url=proxy_url, data=b"")
            req.headers["X-Amz-Content-SHA256"] = hashlib.sha256(b"").hexdigest()
            SigV4Auth(creds.get_frozen_credentials(), "lambda", region).add_auth(req)
            t0 = time.perf_counter()
            r = s_proxy.get(proxy_url, headers=dict(req.headers), timeout=10)
            p.append((time.perf_counter() - t0) * 1000.0)
            pst.add(r.status_code)
        except Exception as e:  # noqa: BLE001
            pst.add(str(e)[:40])
        if i + 1 < n:
            time.sleep(gap)

    print()
    print("=" * 96)
    print("ORDER-PATH A/B — GET /time, direct vs through pmproxy (ms, warm session)")
    print("=" * 96)
    for lbl, xs, st in (("direct clob.polymarket.com", d, dst),
                        (f"pmproxy {region} Lambda", p, pst)):
        if xs:
            print(f"  {lbl:<30} n={len(xs):<3} p50 {q(xs,.5):7.1f}  p90 {q(xs,.9):7.1f}  "
                  f"min {min(xs):7.1f}  max {max(xs):7.1f}   status={sorted(map(str,st))}")
        else:
            print(f"  {lbl:<30} FAILED  {sorted(map(str,st))}")
    if d and p:
        delta = q(p, .5) - q(d, .5)
        print()
        print(f"  ROUTING DELTA (p50): {delta:+.1f} ms "
              f"{'AGAINST' if delta > 0 else 'IN FAVOUR OF'} the proxy.")
        print("  pmengine sends POST /order down whichever of these two the env selects.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ab-only", action="store_true",
                    help="skip the endpoint sweep, run only the order-path A/B")
    ap.add_argument("-n", "--passes", type=int, default=20)
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between passes")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--out", default="", help="write raw samples as JSON here")
    ap.add_argument("--env", default="../.env", help=".env to read PMPROXY_URL from")
    args = ap.parse_args()

    if args.ab_only:
        probe_order_path_ab(args.passes, args.gap, args.env)
        return 0

    eps = list(ENDPOINTS)
    proxy = os.environ.get(PROXY_ENV, "")
    if not proxy and os.path.exists(args.env):
        for line in open(args.env):
            line = line.strip()
            if line.startswith(PROXY_ENV + "="):
                proxy = line.split("=", 1)[1].strip().strip('"')
    if proxy:
        # The proxy's own health path; a bare GET is enough to time the hop.
        eps.append(("pmproxy(lambda)", proxy.rstrip("/") + "/", "https"))

    raw: dict[str, list[dict]] = defaultdict(list)
    print(f"# net_probe {args.passes} passes, {args.gap}s apart, {len(eps)} endpoints",
          file=sys.stderr)
    for i in range(args.passes):
        for label, url, kind in eps:
            r = probe_https(url, args.timeout) if kind == "https" else probe_wss(url, args.timeout)
            r["pass"] = i
            r["t"] = time.time()
            raw[label].append(r)
        print(f"  pass {i+1}/{args.passes}", file=sys.stderr)
        if i + 1 < args.passes:
            time.sleep(args.gap)

    if args.out:
        with open(args.out, "w") as f:
            json.dump({k: v for k, v in raw.items()}, f)

    print()
    print("=" * 96)
    print("NETWORK PROBE — this box to the endpoints that matter (ms)")
    print("=" * 96)
    print(f"host: residential US desktop, {socket.gethostname()}")
    print(f"passes: {args.passes}, spacing {args.gap}s")
    print()
    hdr = f"{'endpoint':<18} {'stage':<12} {'min':>7} {'p50':>7} {'p90':>7} {'max':>7} {'sd':>7}  n"
    print(hdr)
    print("-" * len(hdr))
    for label, _url, kind in eps:
        ss = raw[label]
        ok = [s for s in ss if "error" not in s]
        errs = [s for s in ss if "error" in s]
        stages = (
            [("dns", "dns_ms"), ("tcp", "tcp_ms"), ("tls", "tls_ms"),
             ("http_cold", "http_cold_ms"), ("http_warm", "http_warm_ms")]
            if kind == "https"
            else [("dns", "dns_ms"), ("tcp", "tcp_ms"), ("tls", "tls_ms"),
                  ("ws_upgrade", "upgrade_ms")]
        )
        for name, key in stages:
            n = len([s for s in ok if key in s])
            print(f"{label:<18} {name:<12} {summarize(ok, key)}  {n}")
        st = {s.get("status") for s in ok}
        ip = {s.get("ip") for s in ok}
        note = f"  status={sorted(x for x in st if x)} ip={sorted(x for x in ip if x)}"
        if errs:
            note += f" ERRORS={len(errs)}: {errs[0]['error'][:70]}"
        print(f"{'':<18} {note}")
        print()

    # The single number the verdict needs: warm HTTP RTT to the order host.
    order = [s.get("http_warm_ms") for s in raw["clob.book"] if s.get("http_warm_ms")]
    if order:
        print("-" * 96)
        print(f"ORDER-HOST WARM RTT (clob.polymarket.com, pooled connection): "
              f"p50 {q(order,0.5):.1f}ms  p90 {q(order,0.9):.1f}ms  max {max(order):.1f}ms")
        print("A signed order POST costs this plus signing plus server-side matching.")
    probe_order_path_ab(min(args.passes, 15), max(1.0, args.gap), args.env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
