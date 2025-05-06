#!/bin/bash

# Check for required environment variables
if [ -z "$GOOGLE_API_KEY" ]; then
    echo "Error: GOOGLE_API_KEY environment variable is not set"
    exit 1
fi

# Create logs directory if it doesn't exist
mkdir -p logs

# Install system dependencies for Chromium
echo "Installing system dependencies..."
apt-get update && apt-get install -y \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libexpat1 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libgtk-3-0

# Install Python dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Install Playwright if not already installed
echo "Installing/Updating Playwright..."
pip install playwright --upgrade

# Install Playwright browser dependencies
echo "Installing Playwright browser dependencies..."
playwright install chromium

# Make restart script executable
chmod +x restart_monitor.sh

# Start the application with auto-restart capability
echo "Starting appointment monitor with auto-restart capability..."
exec ./restart_monitor.sh 