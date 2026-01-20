#!/bin/bash
# Integration tests for pmengine
#
# These tests verify that pmengine can:
# 1. Fetch markets from Gamma API
# 2. Parse market data correctly
#
# No CLOB authentication needed for these tests.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PMENGINE_DIR="$(dirname "$SCRIPT_DIR")"
ROOT_DIR="$(dirname "$PMENGINE_DIR")"

cd "$PMENGINE_DIR"

echo "=== pmengine Integration Tests ==="
echo ""

# Test 1: Gamma API connectivity
echo "Test 1: Gamma API connectivity (--test-gamma)"
echo "--------------------------------------------"

# Build first
echo "Building pmengine..."
cargo build --features ec2 --quiet 2>/dev/null || cargo build --quiet

# Run the test-gamma command
echo "Running Gamma API test..."
if cargo run --quiet -- --test-gamma 2>&1; then
    echo ""
    echo "✓ Test 1 PASSED: Gamma API is accessible"
else
    echo ""
    echo "✗ Test 1 FAILED: Gamma API fetch failed"
    exit 1
fi

echo ""

# Test 2: Unit tests
echo "Test 2: Rust unit tests"
echo "-----------------------"
if cargo test --quiet; then
    echo "✓ Test 2 PASSED: All unit tests pass"
else
    echo "✗ Test 2 FAILED: Unit tests failed"
    exit 1
fi

echo ""
echo "=== All Integration Tests Passed ==="
