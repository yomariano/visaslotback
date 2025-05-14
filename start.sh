#!/bin/bash
set -e  # Exit on error

# Create logs directory if it doesn't exist
mkdir -p logs

# Detect operating system
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS system
    echo "Detected macOS environment"
    
    # Check if Homebrew is installed
    if ! command -v brew &> /dev/null; then
        echo "Homebrew not found. Please install Homebrew first: https://brew.sh/"
        echo "Then run this script again."
        exit 1
    fi
    
    # Install Chrome on macOS if needed
    if ! command -v google-chrome &> /dev/null && ! command -v "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" &> /dev/null; then
        echo "Installing Google Chrome via Homebrew..."
        brew install --cask google-chrome
    fi
    
    # Set Chrome path for macOS
    if command -v "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" &> /dev/null; then
        export CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    else
        echo "Warning: Google Chrome not found. Please install it manually."
    fi
    
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    # Linux system
    echo "Detected Linux environment"
    
    # Check if we have sudo/root permissions for apt
    if command -v apt-get &> /dev/null && [ $(id -u) -eq 0 ]; then
        echo "Installing build tools and system dependencies..."
        apt-get update && apt-get install -y \
            wget \
            gnupg \
            build-essential \
            python3-dev \
            libffi-dev \
            libssl-dev \
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
        # Clean up existing Chrome source list file to prevent duplicates
        if [ -f /etc/apt/sources.list.d/google-chrome.list ]; then
            echo "Removing existing Google Chrome source list..."
            rm -f /etc/apt/sources.list.d/google-chrome.list
        fi

        # Add the Chrome repository correctly
        wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
        echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
        apt-get update && apt-get install -y google-chrome-stable
        
        # Set Chrome path
        export CHROME_PATH=$(which google-chrome-stable)
    else
        echo "Warning: Not running as root or apt-get not available."
        echo "Please ensure Chrome is installed manually."
        
        # Try to locate Chrome
        if command -v google-chrome &> /dev/null; then
            export CHROME_PATH=$(which google-chrome)
        elif command -v google-chrome-stable &> /dev/null; then
            export CHROME_PATH=$(which google-chrome-stable)
        else
            echo "Warning: Google Chrome not found. Please install it manually."
        fi
    fi
else
    echo "Unsupported operating system: $OSTYPE"
    echo "Please ensure Chrome is installed manually."
fi

# Upgrade pip and install wheel
echo "Upgrading pip and installing wheel..."
pip install --upgrade pip wheel setuptools

# Install Python dependencies with error handling
echo "Installing Python dependencies..."
if ! pip install --prefer-binary --use-pep517 -r requirements.txt; then
    echo "Error: Failed to install Python dependencies"
    exit 1
fi

# Make scripts executable
chmod +x run_monitor.py
chmod +x restart_monitor.sh

# Report Chrome path
if [ -n "$CHROME_PATH" ]; then
    echo "Using Chrome at: $CHROME_PATH"
else
    echo "Warning: Chrome path not set. The application may not work correctly."
fi

# Start the application with auto-restart capability
echo "Starting appointment monitor with auto-restart capability..."
exec ./run_monitor.py 