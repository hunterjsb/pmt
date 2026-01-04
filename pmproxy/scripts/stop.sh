#!/bin/bash
set -e

echo "Stopping pmproxy..."
pkill -f pmproxy || true
sleep 2
echo "pmproxy stopped"
