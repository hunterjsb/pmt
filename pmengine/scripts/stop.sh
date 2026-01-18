#!/bin/bash
set -e

echo "Stopping pmengine..."
pkill -f pmengine || true
sleep 2

# Remove old binary so CodeDeploy can install new one
rm -f /usr/local/bin/pmengine

echo "pmengine stopped"
