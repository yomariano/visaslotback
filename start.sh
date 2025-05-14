#!/bin/bash

# Check for required environment variables
if [ -z "$OPENAI_API_KEY" ]; then
    echo "Error: OPENAI_API_KEY environment variable is not set"
    exit 1
fi

# Create logs directory if it doesn't exist
mkdir -p logs

# Install system dependencies for Chrome
echo "Installing system dependencies for Chrome..."
apt-get update && apt-get install -y \
    wget \
    gnupg \
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

# Download and install Google Chrome
echo "Installing Google Chrome..."
wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list
apt-get update && apt-get install -y google-chrome-stable

# Install Python dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Make scripts executable
chmod +x run_monitor.py
chmod +x restart_monitor.sh

# Set Chrome path in environment
export CHROME_PATH=$(which google-chrome-stable)
echo "Using Chrome at: $CHROME_PATH"

# Start the application with auto-restart capability
echo "Starting appointment monitor with auto-restart capability..."
exec ./run_monitor.py 