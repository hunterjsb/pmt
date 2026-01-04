#!/bin/bash
set -e

echo "Starting pmproxy..."
nohup /usr/local/bin/pmproxy --host 127.0.0.1 --port 8080 > /home/ec2-user/pmproxy.log 2>&1 &
disown
echo "pmproxy started with PID $!"
