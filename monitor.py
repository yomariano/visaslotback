"""
Simple try of the agent.

@dev You need to add OPENAI_API_KEY to your environment variables.
You can also set CHROME_USER_DATA and CHROME_PERSISTENT_SESSION to use your personal Chrome session.
"""

import os
import sys
import asyncio
import time
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
    model="gpt-4o",
    api_key=os.getenv("OPENAI_API_KEY")
)

# Configure browser with explicit options
browser_context_config = BrowserContextConfig(
    disable_security=False,
    save_downloads_path="/Users/mariano/Downloads",
)

browser = Browser(
	config=BrowserConfig(
		headless=True,  # Set to headless mode
		disable_security=False,
		new_context_config=browser_context_config,
	)
)

async def run_agent_task():
    # Create and run the agent task
    agent = Agent(
        task="""
        - Go to https://schengenappointments.com/
        - Check the dropdown at the top with all the cities and save the list of cities in a variable
        - pick from the top dropdown the city you want to check
        - analyze the table of slots and bring me back that information in json format like this: {
                "city": "",
                "countries": [
                    {
                        "country": "",
                        "flag": "",
                        "earliest_available": "",
                        "url": "",
                        "slots": {
                            "MAY": "",
                            "JUN": "",
                            "JUL": ""
                        }
                    }
                ],
                "temporarily_unavailable": [],
                "timestamp": ""
            }
        - do not stop until you have checked all the cities
        """,
        llm=llm,
        browser=browser,
    )
    
    # Run the agent task
    await agent.run(max_steps=150)

async def main():
    while True:
        print(f"Starting agent task at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        try:
            # Run the agent task
            await run_agent_task()
        except Exception as e:
            print(f"Error occurred: {e}")
        
        print(f"Task completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Waiting 3 minutes before running again...")
        
        # Wait for 3 minutes (180 seconds)
        await asyncio.sleep(180)

if __name__ == '__main__':
	asyncio.run(main())