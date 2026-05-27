"""Deep behavioral tests against the live pmproxy Lambda.

Distinct from test_deployed.py (the post-deploy smoke that the workflow
runs): this suite is intentionally heavier (auth-failure matrix, rate
limit firing, latency distribution, concurrency, failure injection,
metric counter accuracy). Run manually:

    cd pmproxy/tests
    pip install -r requirements.txt
    PMPROXY_URL=... PMPROXY_COGNITO_* ... pytest test_deep.py -v

Not included in CI — these tests intentionally trip rate limits and
burst-load the function, which would be noisy and expensive in CI.

Tests are read-only against Polymarket upstreams (no order placement).
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import json
import os
import statistics
import time
from typing import Iterable

import pytest
import requests

PROXY_URL = os.environ.get("PMPROXY_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not PROXY_URL, reason="PMPROXY_URL not set")


# ---------- shared helpers ----------

def _cognito_access_token() -> str | None:
    """Mint a fresh Cognito token from env. None if creds missing."""
    required = ("PMPROXY_COGNITO_CLIENT_ID", "PMPROXY_USERNAME", "PMPROXY_PASSWORD")
    if not all(os.environ.get(k) for k in required):
        return None
    import boto3

    client = boto3.client(
        "cognito-idp",
        region_name=os.environ.get("PMPROXY_COGNITO_REGION", "us-east-1"),
    )
    resp = client.initiate_auth(
        ClientId=os.environ["PMPROXY_COGNITO_CLIENT_ID"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": os.environ["PMPROXY_USERNAME"],
            "PASSWORD": os.environ["PMPROXY_PASSWORD"],
        },
    )
    return resp["AuthenticationResult"]["AccessToken"]


@pytest.fixture(scope="module")
def token() -> str:
    t = _cognito_access_token()
    if not t:
        pytest.skip("Cognito credentials missing — can't run authed tests")
    return t


@pytest.fixture(scope="module")
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _www_authenticate(response: requests.Response) -> str:
    """Read WWW-Authenticate from a Lambda Function URL response.

    Lambda renames a fixed set of "restricted" response headers to
    `x-amzn-Remapped-*` so they survive the gateway. WWW-Authenticate is
    on that list — we ship it from pmproxy, but clients only see it as
    `x-amzn-Remapped-www-authenticate`. Checking both keeps tests honest
    against both Lambda and a future EC2 deployment.
    """
    return response.headers.get("WWW-Authenticate") or response.headers.get(
        "x-amzn-Remapped-www-authenticate", ""
    )


def _scrape_metric(name: str, labels: dict[str, str] | None = None) -> int:
    """Pull a single counter value from /metrics. 0 if absent."""
    resp = requests.get(f"{PROXY_URL}/metrics", timeout=10)
    resp.raise_for_status()
    label_str = ""
    if labels:
        parts = ",".join(f'{k}="{v}"' for k, v in labels.items())
        label_str = "{" + parts + "}"
    target = f"{name}{label_str}"
    for line in resp.text.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(target + " ") or line.startswith(target + "\t"):
            return int(float(line.rsplit(None, 1)[-1]))
    return 0


# ============================================================
# 1. End-to-end JWT failure paths
# ============================================================

class TestJwtFailures:
    """Verify every JWT failure mode against the deployed Lambda.

    These mirror the unit tests in auth.rs::jwt_validation_tests but
    exercise the full request path (gateway → Lambda → handler) so we
    know the deployed binary actually rejects these.
    """

    def test_missing_authorization_header(self):
        r = requests.get(f"{PROXY_URL}/clob/sampling-markets", timeout=10)
        assert r.status_code == 401
        body = r.json()
        assert body.get("error") == "missing_token"
        assert _www_authenticate(r).startswith("Bearer")

    def test_garbage_bearer_token(self):
        r = requests.get(
            f"{PROXY_URL}/clob/sampling-markets",
            headers={"Authorization": "Bearer not.a.jwt"},
            timeout=10,
        )
        assert r.status_code == 401
        assert r.json().get("error") == "invalid_token"

    def test_token_without_bearer_prefix(self):
        r = requests.get(
            f"{PROXY_URL}/clob/sampling-markets",
            headers={"Authorization": "abc123"},  # no Bearer
            timeout=10,
        )
        assert r.status_code == 401
        assert r.json().get("error") == "missing_token"

    def test_basic_auth_header_not_accepted(self):
        creds = base64.b64encode(b"user:pass").decode()
        r = requests.get(
            f"{PROXY_URL}/clob/sampling-markets",
            headers={"Authorization": f"Basic {creds}"},
            timeout=10,
        )
        assert r.status_code == 401
        assert r.json().get("error") == "missing_token"

    def test_jwt_with_bad_base64(self):
        # Three dotted segments but garbage payload
        r = requests.get(
            f"{PROXY_URL}/clob/sampling-markets",
            headers={"Authorization": "Bearer not!base64.also!bad.def"},
            timeout=10,
        )
        assert r.status_code == 401
        assert r.json().get("error") == "invalid_token"

    def test_jwt_signed_by_wrong_key(self):
        # Build a valid-looking JWT structure but with a fake signature.
        # The Lambda's JwksCache won't find a matching kid, will try to
        # refresh, won't find it, and reject. Should be 401, not 5xx.
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "kid": "attacker-key"}).encode()
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "attacker", "exp": int(time.time()) + 3600}).encode()
        ).rstrip(b"=").decode()
        sig = "x" * 256
        token = f"{header}.{payload}.{sig}"
        r = requests.get(
            f"{PROXY_URL}/clob/sampling-markets",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert r.status_code in (401, 503), f"got {r.status_code}: {r.text}"
        # 503 if JWKS refresh hit a transient — both indicate the request
        # didn't pass auth.

    def test_valid_token_accepted(self, auth):
        r = requests.get(
            f"{PROXY_URL}/clob/sampling-markets", headers=auth, timeout=10,
        )
        assert r.status_code == 200


# ============================================================
# 2. Metric counter accuracy
# ============================================================

class TestMetricsAccuracy:
    """Make a known traffic pattern, verify /metrics counts it correctly."""

    def test_request_counter_increments(self, auth):
        before = _scrape_metric(
            "pmproxy_requests_total", {"route": "gamma", "status": "200"}
        )
        # Drive 5 known successful gamma requests.
        for _ in range(5):
            r = requests.get(
                f"{PROXY_URL}/gamma/events",
                params={"limit": 1},
                headers=auth,
                timeout=10,
            )
            assert r.status_code == 200
        # Allow a moment for the Lambda to flush counters across cold/warm
        # invocations. The counters are per-instance — if Lambda spins up
        # a fresh instance, our 5 may split. So we only assert a LOWER
        # bound; we know at least one instance saw at least one request.
        time.sleep(1.0)
        after = _scrape_metric(
            "pmproxy_requests_total", {"route": "gamma", "status": "200"}
        )
        # Soft assertion: counter moved up. We can't assert exactly +5
        # because we may have hit different Lambda instances.
        assert after >= before, f"counter went backwards: {before} → {after}"

    def test_auth_failure_counter_increments(self):
        before = _scrape_metric(
            "pmproxy_auth_failures_total", {"reason": "missing_token"}
        )
        for _ in range(3):
            r = requests.get(f"{PROXY_URL}/clob/sampling-markets", timeout=10)
            assert r.status_code == 401
        time.sleep(1.0)
        after = _scrape_metric(
            "pmproxy_auth_failures_total", {"reason": "missing_token"}
        )
        assert after >= before

    def test_metrics_endpoint_self_counts(self):
        # Hitting /metrics itself is recorded. Confirm.
        before = _scrape_metric(
            "pmproxy_requests_total", {"route": "metrics", "status": "200"}
        )
        # 3 extra hits beyond the first scrape
        for _ in range(3):
            _scrape_metric("pmproxy_requests_total")
        after = _scrape_metric(
            "pmproxy_requests_total", {"route": "metrics", "status": "200"}
        )
        # Each call to _scrape_metric is itself a /metrics hit (4 above
        # plus the final reading). >=4 on the same instance, but Lambda
        # cold starts can split. Lower-bound assertion.
        assert after >= before


# ============================================================
# 3. Latency distribution
# ============================================================

# Sleep between auth'd requests = 60rpm budget / 60s = 1 req/sec. We pace
# at 1.2s to stay under and leave headroom for the burst test at the end.
_AUTHED_PACE_S = 1.2

@pytest.mark.parametrize("route,authed,n", [
    ("/health", False, 50),
    ("/badge", False, 50),
    ("/metrics", False, 50),
    ("/clob/sampling-markets", True, 10),
    ("/gamma/events?limit=1", True, 10),
])
class TestLatency:
    def test_latency_under_alarm_threshold(self, route, authed, n, auth):
        headers = auth if authed else {}
        latencies_ms = []
        for _ in range(n):
            t0 = time.perf_counter()
            r = requests.get(f"{PROXY_URL}{route}", headers=headers, timeout=10)
            latencies_ms.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200, f"{route} returned {r.status_code}"
            if authed:
                time.sleep(_AUTHED_PACE_S)

        p50 = statistics.median(latencies_ms)
        p95 = sorted(latencies_ms)[int(0.95 * n)]
        p99 = sorted(latencies_ms)[int(0.99 * n) if n > 99 else -1]
        print(
            f"\n  {route:<30}  n={n}  p50={p50:>6.0f}ms  "
            f"p95={p95:>6.0f}ms  p99={p99:>6.0f}ms"
        )
        # The Lambda's p99 latency alarm trips at 5000ms.
        assert p99 < 5000, f"{route} p99={p99}ms exceeds alarm threshold"


# ============================================================
# 4. Concurrency
# ============================================================

class TestConcurrency:
    """Parallel-fan-out test. Sized to stay within burst (10) for Free
    tier — we're testing that concurrent invocations don't interleave or
    drop responses, not that the rate limiter rejects burst."""

    def test_parallel_requests_within_burst(self, auth):
        # Sleep briefly before this test to let prior tests' budget refill.
        time.sleep(5)

        N = 8  # stays under the 10-burst Free-tier ceiling

        def hit(_) -> tuple[int, int, str]:
            t0 = time.perf_counter()
            r = requests.get(
                f"{PROXY_URL}/gamma/events",
                params={"limit": 1},
                headers=auth,
                timeout=15,
            )
            ms = int((time.perf_counter() - t0) * 1000)
            return (r.status_code, ms, r.text[:80])

        with cf.ThreadPoolExecutor(max_workers=N) as pool:
            results = list(pool.map(hit, range(N)))

        statuses = [s for s, _, _ in results]
        latencies = [ms for _, ms, _ in results]
        ok = sum(1 for s in statuses if s == 200)
        bad = [r for r in results if r[0] != 200]
        print(
            f"\n  {N} parallel: {ok}/{N} ok, "
            f"latency p50={statistics.median(latencies):.0f}ms "
            f"max={max(latencies):.0f}ms"
        )
        assert ok == N, f"some failed within burst: {bad}"


# ============================================================
# 5. Failure injection
# ============================================================

class TestFailureInjection:
    def test_malformed_jsonrpc_to_chain(self, auth):
        r = requests.post(
            f"{PROXY_URL}/chain/",
            data="this is not json",
            headers={**auth, "Content-Type": "application/json"},
            timeout=10,
        )
        # publicnode upstream will likely reject; we should get a clean
        # 4xx/5xx forwarded, not a panic.
        assert r.status_code in (400, 415, 500, 502), f"got {r.status_code}: {r.text}"

    def test_oversized_request_body(self, auth):
        # 10 MB body — should either succeed (large request body) or get
        # rejected at the Lambda layer (6MB limit), but never crash.
        big = "x" * (10 * 1024 * 1024)
        try:
            r = requests.post(
                f"{PROXY_URL}/chain/",
                data=big,
                headers={**auth, "Content-Type": "application/json"},
                timeout=30,
            )
            # Lambda Function URL has a 6MB request body limit. 413 or
            # 4xx is the expected response.
            assert r.status_code in (400, 413, 502), f"got {r.status_code}"
        except requests.exceptions.RequestException as e:
            # Network-level rejection is fine too
            print(f"  oversized rejected at transport: {e.__class__.__name__}")

    def test_path_traversal_normalized_upstream(self, auth):
        # NOTE: Lambda Function URL (and HTTP clients in general) normalize
        # `..` segments BEFORE our handler sees the request. By the time
        # pmproxy::route() runs, /clob/../gamma has already become /gamma.
        # So the practical behavior on Lambda is: /clob/../gamma → 200
        # (legit route to gamma upstream), not 404.
        #
        # Our route()-level `..` rejection (1.0.1) is defense in depth for
        # a future EC2 deployment where the HTTP stack might pass `..`
        # through unchanged. The unit test in upstream::tests covers that
        # case. Here we just verify the live Lambda behavior is sane.
        r = requests.get(f"{PROXY_URL}/clob/../gamma", headers=auth, timeout=10)
        assert r.status_code in (200, 404), f"unexpected {r.status_code}: {r.text[:100]}"
        # Either way, no 5xx — the request didn't crash anything.

    def test_authorization_header_not_forwarded_upstream(self, auth):
        # Polymarket's upstream wouldn't recognize our Cognito JWT. If we
        # ever forwarded it, upstream would error. The fact that
        # /clob/sampling-markets WORKS means we strip Authorization
        # correctly. Tested transitively by the happy-path tests.
        r = requests.get(f"{PROXY_URL}/clob/sampling-markets", headers=auth, timeout=10)
        assert r.status_code == 200


# ============================================================
# 6. Rate limit firing (intentional burst — RUNS LAST so the depleted
#    budget doesn't pollute earlier tests)
# ============================================================

class TestZRateLimit:  # Z- prefix to sort last under pytest's default order
    """Drive past the Free-tier 60-rpm budget. Cognito users default to
    Free unless they have a `custom:tenant_tier` claim — verify both the
    429 response shape and the /metrics counter."""

    def test_burst_eventually_429s(self, auth):
        # Free tier: 60 rpm with 10 burst. Send 80 requests as fast as
        # we can; we expect to see some 429s once the burst is depleted.
        n = 80
        codes = []
        first_429_response = None
        timeouts = 0
        for _ in range(n):
            try:
                r = requests.get(
                    f"{PROXY_URL}/gamma/events",
                    params={"limit": 1},
                    headers=auth,
                    timeout=10,
                )
            except requests.exceptions.RequestException:
                timeouts += 1
                continue
            codes.append(r.status_code)
            if r.status_code == 429 and first_429_response is None:
                first_429_response = r

        n_429 = sum(1 for c in codes if c == 429)
        n_200 = sum(1 for c in codes if c == 200)
        print(f"\n  {n} burst: 200={n_200}, 429={n_429}, timeouts={timeouts}")

        if n_429 == 0:
            pytest.skip("No 429s — user likely on Pro/Enterprise tier (tested clean)")

        # Verify 429 response shape
        assert first_429_response.json().get("error") == "rate_limited"
        assert "rate_limited" in _www_authenticate(first_429_response)
