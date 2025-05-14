# Visa Slot Monitor: Browser-Use Agent Implementation

## Overview

This README documents the migration from the crawl4ai library to the browser_use agent for the Visa Slot Monitor application. The new implementation leverages the `browser_use` package with OpenAI's LLM to perform autonomous web browsing and data extraction.

## Key Changes

### Architecture

- **Replaced crawl4ai with browser_use**: The application now uses the `browser_use` package with LangChain integration for web scraping
- **Agent-based approach**: Leverages an LLM agent that can navigate websites and extract structured data
- **Enhanced resilience**: Improved memory management and error handling to prevent crashes
- **Batch processing**: Cities are processed in batches with browser reinitialization between batches

### Dependencies

- Added `langchain-openai` for LLM integration
- Removed `crawl4ai` dependency
- Changed from Google API to OpenAI API for LLM functionality

### Key Files

1. **appointment_monitor.py**: Main application reworked to use browser_use agent
2. **run_monitor.py**: New entry-point script
3. **test_browser_agent.py**: Test script for isolated testing of the agent functionality
4. **start.sh**: Updated to install and configure Chrome browser
5. **restart_monitor.sh**: Updated to manage the new implementation

## Running the Application

### Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set required environment variables:
```bash
export OPENAI_API_KEY=your_openai_api_key
export CHROME_PATH=/path/to/chrome  # Optional, auto-detected in most cases
```

3. Run the application:
```bash
# Direct execution
python run_monitor.py

# Using the start script (for production)
./start.sh
```

### Testing

To test the browser agent with a single city:
```bash
python test_browser_agent.py
```

## Agent Workflow

1. **Initialization**: Creates a new browser instance for each monitoring cycle
2. **Navigation**: Agent navigates to the appointment website and selects a city
3. **Data Extraction**: Agent analyzes the page, extracts slot information in JSON format
4. **Comparison**: System compares new data with previous data to detect changes
5. **Notification**: Triggers notifications based on detected changes
6. **Cleanup**: Browser is closed between batches to prevent resource leaks

## Troubleshooting

### Browser Issues

- **Chrome not found**: Set `CHROME_PATH` environment variable
- **Headless mode failures**: Set `headless=False` in the BrowserConfig for debugging
- **Timeouts**: Adjust the timeout values in the `extract_appointment_data` method

### Agent Issues

- **Parsing errors**: The agent may sometimes return malformed JSON. Check the logs for error details.
- **Navigation failures**: If the agent fails to navigate, try increasing the `max_steps` parameter
- **Memory errors**: If you see out-of-memory errors, reduce the batch size

## Advantages Over Previous Implementation

1. **Reliability**: More reliable scraping with fewer timeouts and errors
2. **Flexibility**: Agent can adapt to minor website changes without code updates
3. **Maintainability**: Simplified code structure with better separation of concerns
4. **Resource usage**: Improved memory management with forced cleanup
5. **Error handling**: Better error recovery with specific handling for different failure types 