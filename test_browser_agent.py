import os
import sys
import asyncio
import json
from datetime import datetime

from langchain_openai import ChatOpenAI
from browser_use import Agent, Browser, BrowserConfig
from browser_use.browser.context import BrowserContextConfig
from dotenv import load_dotenv
from loguru import logger

# Load environment variables
load_dotenv()

# Configure logging
logger.add(
    "logs/test_browser_agent_{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention="3 days",
    level="INFO"
)

async def extract_appointment_data(city: str):
    """Test the extraction of appointment data for a specific city."""
    # Set up browser configuration
    browser_context_config = BrowserContextConfig(
        disable_security=False,
        save_downloads_path=os.path.join(os.getcwd(), "downloads"),
    )
    
    browser = Browser(
        config=BrowserConfig(
            headless=True,  # Set to False to see the browser in action
            disable_security=False,
            new_context_config=browser_context_config,
        )
    )
    
    # Initialize LLM
    llm = ChatOpenAI(
        model="gpt-4o",
        api_key=os.getenv("OPENAI_API_KEY")
    )
    
    # Generate the city URL
    base_url = "https://schengenappointments.com"
    city_formatted = city.lower().replace(" ", "-")
    city_url = f"{base_url}/in/{city_formatted}/tourism"
    
    logger.info(f"Testing appointment data extraction for {city} at {city_url}")
    
    # Get current and next two months
    current_month = datetime.now().month
    tracked_months = []
    for i in range(3):
        month = (current_month + i) % 12
        if month == 0:
            month = 12
        tracked_months.append(datetime.strptime(str(month), "%m").strftime("%b").upper())
    
    # Set up the agent task
    agent_task = f"""
    - Go to {city_url}
    - Analyze the table of slots and extract the following information:
      * For each country with available slots:
        - Country name
        - Flag emoji
        - Earliest available date
        - Number of slots for each month ({", ".join(tracked_months)})
      * For countries with no availability, add them to a separate list
    - Format the data as a detailed JSON structure with the following keys:
      * countries: Array of countries with available slots
      * temporarily_unavailable: Array of country names with no slots
    - Be precise with the data extraction, especially dates and slot numbers
    """
    
    agent = Agent(
        task=agent_task,
        llm=llm,
        browser=browser,
    )
    
    try:
        # Run the agent with a timeout
        result = await asyncio.wait_for(
            agent.run(max_steps=50),
            timeout=120  # 2 minute timeout
        )
        
        logger.info(f"Agent task completed for {city}")
        
        # Get the result - the agent stores the result in the 'result' field
        # The actual result content should be in the log output
        agent_result = result.result if hasattr(result, 'result') else str(result)
        logger.info(f"Agent result: {agent_result}")
        
        # Extract JSON from agent's output
        json_content = extract_json_from_text(agent_result)
        if json_content:
            # Parse and format the JSON for better readability
            data = json.loads(json_content)
            formatted_json = json.dumps(data, indent=2)
            logger.info(f"Extracted JSON data:\n{formatted_json}")
            
            # Save the data to a file
            with open(f"test_data_{city.lower().replace(' ', '_')}.json", "w") as f:
                f.write(formatted_json)
            
            logger.info(f"Data saved to test_data_{city.lower().replace(' ', '_')}.json")
        else:
            logger.warning(f"No JSON data found in agent's result")
            
    except asyncio.TimeoutError:
        logger.error(f"Timeout extracting appointment data for {city}")
    except Exception as e:
        logger.error(f"Error running agent for {city}: {e}")
    finally:
        # Clean up
        try:
            await browser.close()
            logger.info("Browser resources cleaned up")
        except Exception as e:
            logger.error(f"Error closing browser: {e}")

def extract_json_from_text(text):
    """Extract JSON content from text, handling different formats."""
    import re
    
    # Check for JSON block in markdown format
    json_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    matches = re.findall(json_pattern, text, re.MULTILINE)
    
    if matches:
        return matches[0].strip()
    
    # Look for JSON-like structure without markdown formatting
    json_start = text.find('{')
    json_end = text.rfind('}')
    
    if json_start >= 0 and json_end > json_start:
        potential_json = text[json_start:json_end+1].strip()
        try:
            # Validate if it's actually JSON
            json.loads(potential_json)
            return potential_json
        except:
            pass
    
    return None

async def main():
    """Main function to test the appointment data extraction."""
    # Test with a few cities
    test_cities = ["London", "New York"]
    
    for city in test_cities:
        await extract_appointment_data(city)
        # Add a pause between cities
        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main()) 