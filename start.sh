#!/bin/bash
set -e

echo "Waiting for Local Bot API on localhost:8081..."
for i in {1..60}; do
    if curl -s http://localhost:8081 > /dev/null 2>&1; then
        echo "Local Bot API is ready"
        break
    fi
    sleep 1
done

echo "Starting bot..."
exec python bot.py
