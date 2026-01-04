#!/bin/bash
set -e

echo "Validating pmproxy..."
sleep 3

# Check if process is running
if ! pgrep -f pmproxy > /dev/null; then
    echo "ERROR: pmproxy process not running"
    exit 1
fi

# Health check
if ! curl -sf http://127.0.0.1:8080/gamma/events?limit=1 > /dev/null; then
    echo "ERROR: Health check failed"
    exit 1
fi

echo "pmproxy validated successfully"
