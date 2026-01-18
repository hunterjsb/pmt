#!/bin/bash
set -e

echo "Starting pmengine..."

# Load environment variables from .env if it exists
if [ -f /home/ec2-user/.pmengine.env ]; then
    set -a
    source /home/ec2-user/.pmengine.env
    set +a
fi

nohup /usr/local/bin/pmengine --log-level info > /home/ec2-user/pmengine.log 2>&1 &
disown
echo "pmengine started with PID $!"
