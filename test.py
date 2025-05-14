"""
Simple try of the agent.

@dev You need to add OPENAI_API_KEY to your environment variables.
You can also set CHROME_USER_DATA and CHROME_PERSISTENT_SESSION to use your personal Chrome session.
"""

import os
import sys
import asyncio
import time
import random
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_openai import ChatOpenAI
from browser_use import Agent, Browser, BrowserConfig
from browser_use.browser.context import BrowserContextConfig
from openai import OpenAI

# Set path to Chrome executable (if not already set)
if "CHROME_PATH" not in os.environ:
    # macOS path
    os.environ["CHROME_PATH"] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    # Windows path would be something like:
    # os.environ["CHROME_PATH"] = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    # Linux path would be something like:
    # os.environ["CHROME_PATH"] = "/usr/bin/google-chrome"

# Set Chrome user data directory for personal profile (if not already set)
if "CHROME_USER_DATA" not in os.environ:
    # macOS path
    os.environ["CHROME_USER_DATA"] = "/Users/mariano/Library/Application Support/Google/Chrome"
    # Windows path would be something like:
    # os.environ["CHROME_USER_DATA"] = "C:\\Users\\mariano\\AppData\\Local\\Google\\Chrome\\User Data"

# Set persistent session to keep browser open between tasks
if "CHROME_PERSISTENT_SESSION" not in os.environ:
    os.environ["CHROME_PERSISTENT_SESSION"] = "true"

# Initialize LLM with OpenAI directly (supports tool use)
llm = ChatOpenAI(
    model="gpt-4o-mini",
    api_key=os.getenv("OPENAI_API_KEY"),
    temperature=0.2  # Lower temperature for more predictable behavior
)

# Configure browser with enhanced options for Cloudflare bypass
browser_context_config = BrowserContextConfig(
    disable_security=False,
    save_downloads_path="/Users/mariano/Downloads",
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    wait_for_network_idle_page_load_time=10.0,  # Increase wait time for network idle
    highlight_elements=False,  # Disable highlighting to avoid detection
)

# Define additional browser arguments to set headers and other settings
browser_args = [
    "--disable-web-security",
    "--disable-features=IsolateOrigins,site-per-process",
    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
]

browser = Browser(
    config=BrowserConfig(
        #headless=True,  # Keep headless commented out - Cloudflare often detects headless mode
        disable_security=False,
        new_context_config=browser_context_config,
        extra_chromium_args=browser_args  # Use the correct parameter name
    )
)

async def run_agent_task():
    # Create and run the agent task with improved instructions for handling Cloudflare
    agent = Agent(
        task="""
        - Go to https://visa.vfsglobal.com/gbr/en/mlt/login
        - When the page loads, wait a moment (3-5 seconds) to simulate a human user
        - If you encounter a Cloudflare challenge or security check:
          - Wait patiently for the challenge to process
          - If there's a CAPTCHA, try to solve it or describe what you see
          - If the page is checking your browser, allow this process to complete
        - Move the mouse naturally around the page and scroll slightly up and down
        - After the page fully loads, wait another 2-3 seconds
        - Login with the following credentials:
            - Username: yomariano05@yopmail.com
            - Password: VfsGlobal123!
        - After login, wait for the dashboard to load completely (3-5 seconds)
        - Click on "my bookings" button
        """,
        llm=llm,
        browser=browser,
    )
    
    # Run the agent task with increased flexibility
    await agent.run(max_steps=75)  # Increased steps to allow for challenge solving

async def main():
    while True:
        print(f"Starting agent task at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        try:
            # Run the agent task
            await run_agent_task()
        except Exception as e:
            print(f"Error occurred: {e}")
        
        print(f"Task completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Use variable wait time to appear less bot-like
        wait_time = random.randint(165, 195)  # 2:45 to 3:15 minutes
        print(f"Waiting approximately {wait_time//60} minutes before running again...")
        
        # Variable wait time between runs
        await asyncio.sleep(wait_time)

if __name__ == '__main__':
    asyncio.run(main())