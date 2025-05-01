#!/bin/bash

# Check for required environment variables
if [ -z "$GOOGLE_API_KEY" ]; then
    echo "Error: GOOGLE_API_KEY environment variable is not set"
    exit 1
fi

# Create logs directory if it doesn't exist
mkdir -p logs

# Install Playwright if not already installed
echo "Installing/Updating Playwright..."
pip install playwright --upgrade

# Install Playwright browser dependencies
echo "Installing Playwright browser dependencies..."
playwright install chromium

# Start the application
echo "Starting appointment monitor..."
exec python3 appointment_monitor.py 