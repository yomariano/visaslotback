#!/bin/bash

# Check for required environment variables
if [ -z "$GOOGLE_API_KEY" ]; then
    echo "Error: GOOGLE_API_KEY environment variable is not set"
    exit 1
fi

# Create logs directory if it doesn't exist
mkdir -p logs

# Start the application
exec python3 appointment_monitor.py 