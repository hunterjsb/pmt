#!/bin/bash
set -e

echo "Validating pmengine..."
sleep 3

# Check if process is running
if ! pgrep -f pmengine > /dev/null; then
    echo "ERROR: pmengine process not running"
    exit 1
fi

# Check logs for startup success
if grep -q "Engine initialized" /home/ec2-user/pmengine.log; then
    echo "pmengine validated successfully"
    exit 0
fi

# Allow time for initialization
sleep 5
if grep -q "Starting engine event loop" /home/ec2-user/pmengine.log; then
    echo "pmengine validated successfully"
    exit 0
fi

echo "WARNING: pmengine running but initialization not confirmed"
exit 0
