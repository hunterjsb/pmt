"""Benchmark order placement latency through pmproxy vs direct."""

import os
import time
import statistics
from dotenv import load_dotenv

load_dotenv()

# Sure bet token for benchmarking (97.95% certainty, $5 min order)
# "Will Mike Mazzei win the 2026 Oklahoma Governor Race?" - No
BENCHMARK_TOKEN = "9195110899659241883972610249084210062362453358677460598659980161913003997727"
MIN_ORDER_SIZE = 5.0  # $5 minimum
ORDER_PRICE = 0.98    # Price per share (98 cents)


def benchmark_orders(client, label: str, num_orders: int = 5) -> dict:
    """Benchmark order placement and cancellation.

    Returns dict with timing stats.
    """
    print(f"\n{'='*50}")
    print(f"Benchmarking: {label}")
    print(f"{'='*50}")

    post_times = []
    cancel_times = []

    for i in range(num_orders):
        # Time the order placement
        start = time.perf_counter()
        try:
            result = client.post_order(
                token_id=BENCHMARK_TOKEN,
                price=ORDER_PRICE,
                size=MIN_ORDER_SIZE,
                side="BUY",
            )
            post_time = (time.perf_counter() - start) * 1000  # ms
            post_times.append(post_time)

            order_id = result.get("orderID") or result.get("id")
            print(f"  Order {i+1}: POST {post_time:.1f}ms", end="")

            # Time the cancellation
            if order_id:
                start = time.perf_counter()
                client.cancel(order_id)
                cancel_time = (time.perf_counter() - start) * 1000
                cancel_times.append(cancel_time)
                print(f" | CANCEL {cancel_time:.1f}ms")
            else:
                print(" | No order ID returned")

        except Exception as e:
            print(f"  Order {i+1}: ERROR - {e}")

        time.sleep(0.2)  # Small delay between orders

    stats = {
        "label": label,
        "post_times": post_times,
        "cancel_times": cancel_times,
    }

    if post_times:
        stats["post_avg"] = statistics.mean(post_times)
        stats["post_min"] = min(post_times)
        stats["post_max"] = max(post_times)
        if len(post_times) > 1:
            stats["post_stdev"] = statistics.stdev(post_times)

    if cancel_times:
        stats["cancel_avg"] = statistics.mean(cancel_times)
        stats["cancel_min"] = min(cancel_times)
        stats["cancel_max"] = max(cancel_times)

    return stats


def print_summary(stats: dict):
    """Print benchmark summary."""
    print(f"\n{stats['label']} Summary:")
    print(f"  POST   - avg: {stats.get('post_avg', 0):.1f}ms, "
          f"min: {stats.get('post_min', 0):.1f}ms, "
          f"max: {stats.get('post_max', 0):.1f}ms")
    if "cancel_avg" in stats:
        print(f"  CANCEL - avg: {stats.get('cancel_avg', 0):.1f}ms, "
              f"min: {stats.get('cancel_min', 0):.1f}ms, "
              f"max: {stats.get('cancel_max', 0):.1f}ms")


def main():
    from polymarket.clob import AuthenticatedClob

    private_key = os.environ.get("PM_PRIVATE_KEY")
    funder_address = os.environ.get("PM_FUNDER_ADDRESS")
    signature_type = int(os.environ.get("PM_SIGNATURE_TYPE", "0"))

    if not private_key or not funder_address:
        print("ERROR: Missing PM_PRIVATE_KEY or PM_FUNDER_ADDRESS")
        return

    print("Order Placement Latency Benchmark")
    print(f"Token: {BENCHMARK_TOKEN[:20]}...")
    print(f"Order: BUY {MIN_ORDER_SIZE} shares @ ${ORDER_PRICE}")

    # Test through pmproxy (direct is blocked by Cloudflare from residential IPs)
    proxy_url = os.environ.get("PMPROXY_URL")
    if not proxy_url:
        print("ERROR: PMPROXY_URL not set")
        return

    print(f"\nTesting via PROXY ({proxy_url})...")
    proxy_client = AuthenticatedClob(
        private_key=private_key,
        funder_address=funder_address,
        signature_type=signature_type,
        proxy=True,
    )
    proxy_stats = benchmark_orders(proxy_client, f"PROXY ({proxy_url})", num_orders=10)

    # Summary
    print("\n" + "="*50)
    print("SUMMARY")
    print("="*50)
    print_summary(proxy_stats)


if __name__ == "__main__":
    main()
