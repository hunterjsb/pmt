#!/bin/bash
set -e

echo "Stopping pmproxy..."
pkill -f pmproxy || true
sleep 2

# Remove old binary so CodeDeploy can install new one
rm -f /usr/local/bin/pmproxy

echo "pmproxy stopped"
