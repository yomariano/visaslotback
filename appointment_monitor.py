import os
import sys
import asyncio
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Any
import re
from bson import ObjectId
import gc
import schedule
from dotenv import load_dotenv
from loguru import logger

# Import browser_use components
from langchain_openai import ChatOpenAI
from browser_use import Agent, Browser, BrowserConfig
from browser_use.browser.context import BrowserContextConfig

from notification_service import NotificationService, NotificationData
from mongodb import MongoDBClient

# Load environment variables from .env file only if they're not already set
load_dotenv(override=False)

# Configure logging
logger.add(
    "logs/appointment_monitor_{time:YYYY-MM-DD}.log",
    rotation="00:00",  # Rotate at midnight
    retention="7 days",
    level="INFO"
)

# Custom JSON encoder to handle MongoDB ObjectId
class MongoJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

class AppointmentMonitor:
    def __init__(self):
        self.base_url = "https://schengenappointments.com"
        self.cities_by_country = {
            "Canada": ["Edmonton", "Montreal", "Ottawa", "Toronto", "Vancouver"],
            "Ireland": ["Dublin"],
            "United Arab Emirates": ["Abu Dhabi", "Dubai"],
            "United Kingdom": ["Birmingham", "Cardiff", "Edinburgh", "London", "Manchester"],
            "United States": ["Atlanta", "Boston", "Chicago", "Houston", "Los Angeles", 
            "Miami", "New York", "San Francisco", "Seattle", "Washington DC"]
        }
        
        # Initialize MongoDB client and notification service
        self.db = MongoDBClient()
        self.notification_service = NotificationService()
        
        # Get next three months for slot tracking
        current_month = datetime.now().month
        self.tracked_months = []
        for i in range(3):
            month = (current_month + i) % 12
            if month == 0:
                month = 12
            self.tracked_months.append(datetime.strptime(str(month), "%m").strftime("%b").upper())
        
        # Add monitoring flag to prevent concurrent runs
        self.monitoring_in_progress = False
        self.last_monitoring_start = None
        self.last_monitoring_end = None
        
        # Set browser configuration
        self._setup_browser_config()
        
        # Initialize LLM
        self.llm = ChatOpenAI(
            model="openai/gpt-4.1-nano",
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY")
        )
        
        # Initialize browser
        self.browser = None
        self.agent = None

    def _setup_browser_config(self):
        """Configure browser settings for optimal performance."""
        # Set path to Chrome executable (if not already set)
        if "CHROME_PATH" not in os.environ:
            # macOS path
            if os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
                os.environ["CHROME_PATH"] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            # Linux path
            elif os.path.exists("/usr/bin/google-chrome"):
                os.environ["CHROME_PATH"] = "/usr/bin/google-chrome"
            # Windows path
            elif os.path.exists("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"):
                os.environ["CHROME_PATH"] = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
        
        # Configure browser context
        self.browser_context_config = BrowserContextConfig(
            disable_security=False,
            save_downloads_path=os.path.join(os.getcwd(), "downloads"),
        )
        
    async def initialize_browser(self):
        """Initialize browser instance."""
        if self.browser is not None:
            await self.cleanup_browser()
        
        # Create new browser instance
        self.browser = Browser(
            config=BrowserConfig(
                headless=True,
                disable_security=False,
                new_context_config=self.browser_context_config,
            )
        )
        
        logger.info("Browser initialized successfully")
        return True
        
    async def cleanup_browser(self):
        """Clean up browser resources."""
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
            finally:
                self.browser = None
                self.agent = None
                
        # Force garbage collection
        gc.collect()
        logger.info("Browser resources cleaned up")

    async def cleanup(self):
        """Clean up all resources used by the monitor."""
        await self.cleanup_browser()
        await self.db.close()
        logger.info("All resources cleaned up")

    def get_city_url(self, city: str) -> str:
        """Generate the URL for a specific city's tourism appointments."""
        city_formatted = city.lower().replace(" ", "-")
        url = f"{self.base_url}/in/{city_formatted}/tourism"
        return url
    
    def _extract_json_from_text(self, text: str) -> Optional[str]:
        """Extract JSON content from text, handling different formats."""
        # Check for JSON block in markdown format
        json_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
        matches = re.findall(json_pattern, text, re.MULTILINE)
        
        if matches:
            # Validate each match to ensure it's valid JSON
            for match in matches:
                try:
                    potential_json = match.strip()
                    json.loads(potential_json)
                    return potential_json
                except json.JSONDecodeError:
                    # Continue to next match if this one isn't valid
                    continue
        
        # Look for JSON-like structure without markdown formatting
        json_start = text.find('{')
        json_end = text.rfind('}')
        
        if json_start >= 0 and json_end > json_start:
            potential_json = text[json_start:json_end+1].strip()
            try:
                # Validate if it's actually JSON
                json.loads(potential_json)
                return potential_json
            except json.JSONDecodeError:
                # If not valid, try to clean or fix common issues
                pass
        
        # If we couldn't find valid JSON, try to construct a minimal valid JSON structure
        # from the extracted data about countries and availability
        try:
            # Extract country data using regex patterns
            country_pattern = r"country(?:_name)?[\"']?\s*:\s*[\"']([^\"']+)[\"']"
            countries = re.findall(country_pattern, text)
            
            if countries:
                # Create a minimal valid JSON with extracted country names
                minimal_json = {
                    "countries": [{"country": country} for country in countries],
                    "temporarily_unavailable": []
                }
                return json.dumps(minimal_json)
        except Exception:
            pass
            
        return None

    def _process_country_data(self, city: str, countries_data_from_agent: List[Dict]) -> List[Dict]:
        """
        Processes country data from the agent to ensure it matches the desired format.
        Assumes the agent provides 'country', 'flag', 'earliest_available', 'url', and 'slots' (as an object)
        for each country, with uppercase month keys for slots.
        """
        processed_countries = []
        if not isinstance(countries_data_from_agent, list):
            logger.warning(f"[{city}] Expected a list for countries_data, got {type(countries_data_from_agent)}. Returning empty list.")
            return []

        for country_data in countries_data_from_agent:
            if not isinstance(country_data, dict):
                logger.warning(f"[{city}] Expected a dict for country_data item, got {type(country_data)}. Skipping item.")
                continue

            name = country_data.get("country")
            flag = country_data.get("flag")
            earliest_available = country_data.get("earliest_available")
            url = country_data.get("url")
            slots = country_data.get("slots")

            if not name or not isinstance(name, str):
                logger.warning(f"[{city}] Missing or invalid 'country' field in {country_data}. Skipping.")
                continue

            # Validate and format slots
            formatted_slots = {}
            if not isinstance(slots, dict):
                logger.warning(f"[{city}] Missing or invalid 'slots' field for {name}, setting to empty for tracked months. Data: {slots}")
                for month_key in self.tracked_months: # e.g., "MAY", "JUN", "JUL"
                    formatted_slots[month_key] = None
            else:
                for month_key in self.tracked_months:
                    slot_value = slots.get(month_key) 
                    if slot_value is None and month_key not in slots: 
                         formatted_slots[month_key] = None
                    elif slot_value is not None and not isinstance(slot_value, str):
                        formatted_slots[month_key] = str(slot_value)
                    else:
                        formatted_slots[month_key] = slot_value
            
            # Construct default URL if not provided or invalid
            country_slug = name.lower().replace(' ', '-')
            default_url = f"{self.get_city_url(city)}/{country_slug}"
            current_country_url = url if isinstance(url, str) and url.startswith("http") else default_url

            processed_countries.append({
                "country": name,
                "flag": flag if isinstance(flag, str) else "",
                "earliest_available": earliest_available if isinstance(earliest_available, str) or earliest_available is None else str(earliest_available),
                "url": current_country_url,
                "slots": formatted_slots
            })
        
        return processed_countries

    async def extract_appointment_data(self, city: str) -> Dict:
        """Extract appointment data from the website for a specific city using browser_use Agent."""
        # Initialize basic data object for consistent returns
        data = {
            "city": city,
            "countries": [],
            "temporarily_unavailable": [],
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            city_url = self.get_city_url(city)
            
            if self.browser is None:
                logger.error(f"Browser is None when processing {city}")
                data["error"] = "Browser not available"
                return data
            
            # Initialize agent with specific task for this city
            agent_task = f"""
            - Go to {city_url}
            - Analyze the table of slots and extract the following information:
              * For each country with available slots:
                - Country name
                - Flag emoji
                - Earliest available date
                - Number of slots for each month ({", ".join(self.tracked_months)})
              * For countries with no availability, add them to a separate list
            - Format the data as a detailed JSON structure with the following keys:
              * countries: Array of objects with country, flag, earliest_available, and slots
              * temporarily_unavailable: Array of country names with no slots
            - For each country, make sure to store the country name and flag emoji separately:
              * CORRECT: "country": "France", "flag": "🇫🇷"
              * INCORRECT: "country": "France 🇫🇷"
            - The output must be valid parseable JSON. Make sure all JSON strings and keys are properly quoted with double quotes.
            - After extracting the data, validate that the JSON is properly formatted and complete before providing it.
            """
            
            try:
                self.agent = Agent(
                    task=agent_task,
                    llm=self.llm,
                    browser=self.browser,
                )
                
                # Add a fallback final_answer attribute to the agent to prevent errors
                # This is a workaround for the browser_use library issue
                if not hasattr(self.agent, 'final_answer'):
                    self.agent.final_answer = None
                
                # Run the agent task with timeout
                try:
                    # Wrap the agent.run in a try-except to catch any AttributeError exceptions
                    try:
                        result = await asyncio.wait_for(
                            self.agent.run(max_steps=50),
                            timeout=120  # 2 minute timeout
                        )
                    except AttributeError as attr_error:
                        # Handle the specific AttributeError case
                        logger.error(f"AttributeError during agent execution for {city}: {attr_error}")
                        if "final_answer" in str(attr_error):
                            # This is the specific error we're trying to fix
                            logger.info(f"Applying workaround for final_answer attribute error in {city}")
                            # Provide a default result
                            result = {"result": "{}"}
                        else:
                            # Re-raise other attribute errors
                            raise
                    
                    # Extract the actual data from the agent's response
                    logger.info(f"Agent task completed for {city}")
                    
                    # Process the agent's result to extract structured data
                    try:
                        # Debug the agent result
                        logger.debug(f"Agent result for {city}: {result}")
                        
                        # Find the final result with is_done=True in all_results if available
                        extracted_content = None
                        
                        # Check if result has all_results attribute
                        if hasattr(result, 'all_results') and result.all_results:
                            # First look for completed actions with JSON content
                            for action_result in result.all_results:
                                if hasattr(action_result, 'is_done') and action_result.is_done:
                                    extracted_content = action_result.extracted_content
                                    break
                                
                                # Also look for extracted content that might contain JSON
                                if hasattr(action_result, 'extracted_content'):
                                    content = action_result.extracted_content
                                    if '```json' in content or '{' in content:
                                        # Try to extract JSON from this content
                                        json_content = self._extract_json_from_text(content)
                                        if json_content:
                                            try:
                                                # Test if it's valid JSON
                                                json.loads(json_content)
                                                extracted_content = content
                                                break
                                            except json.JSONDecodeError:
                                                # Continue if not valid
                                                pass
                        
                        # If we found extracted content, try to use it
                        if extracted_content:
                            json_content = self._extract_json_from_text(extracted_content)
                            if json_content:
                                try:
                                    extracted_data = json.loads(json_content)
                                    
                                    # Process the country data to ensure proper format
                                    if "countries" in extracted_data:
                                        # Process countries to separate country name and flag
                                        processed_countries = self._process_country_data(city, extracted_data["countries"])
                                        data["countries"] = processed_countries
                                    
                                    if "temporarily_unavailable" in extracted_data:
                                        data["temporarily_unavailable"] = extracted_data["temporarily_unavailable"]
                                    
                                    logger.info(f"Successfully extracted data for {city} with {len(data['countries'])} countries available")
                                    return data
                                except json.JSONDecodeError as json_err:
                                    logger.error(f"Error decoding JSON from extracted content for {city}: {json_err}")
                                    # Continue with alternative extraction methods
                        
                        # Fallback to previous extraction method
                        # Get the result from the agent's run
                        # Avoid accessing potentially unsafe attributes
                        agent_result = ""
                        if hasattr(result, 'result'):
                            agent_result = result.result
                        elif isinstance(result, dict) and 'result' in result:
                            agent_result = result['result']
                        else:
                            agent_result = str(result)
                            
                        logger.debug(f"Fallback agent result for {city}: {agent_result}")
                        
                        # Check if the answer contains JSON
                        json_content = self._extract_json_from_text(agent_result)
                        if json_content:
                            try:
                                # Parse the extracted JSON
                                extracted_data = json.loads(json_content)
                                
                                # Process the country data to ensure proper format
                                if "countries" in extracted_data:
                                    # Process countries to separate country name and flag
                                    processed_countries = self._process_country_data(city, extracted_data["countries"])
                                    data["countries"] = processed_countries
                                
                                if "temporarily_unavailable" in extracted_data:
                                    data["temporarily_unavailable"] = extracted_data["temporarily_unavailable"]
                                
                                logger.info(f"Successfully extracted data for {city} with {len(data['countries'])} countries available")
                            except json.JSONDecodeError as json_err:
                                logger.error(f"Error decoding JSON from agent result for {city}: {json_err}")
                                data["error"] = f"JSON decode error: {str(json_err)}"
                                data["raw_response"] = agent_result[:500]  # Store first 500 chars of response for debugging
                        else:
                            logger.warning(f"No valid JSON data found in agent's result for {city}")
                            data["error"] = "Failed to parse agent result - no valid JSON found"
                            data["raw_response"] = agent_result[:500]  # Store first 500 chars of response for debugging
                    except Exception as parse_error:
                        logger.error(f"Error parsing agent response for {city}: {parse_error}")
                        data["error"] = f"Parse error: {str(parse_error)}"
                        # Avoid accessing result properties directly in case of error
                        if isinstance(result, str):
                            data["raw_response"] = result[:500]
                        else:
                            data["raw_response"] = str(result)[:500]
                    
                except asyncio.TimeoutError:
                    logger.error(f"Timeout extracting appointment data for {city}")
                    data["error"] = "Extraction timeout"
                except Exception as run_error:
                    logger.error(f"Error running agent for {city}: {run_error}")
                    data["error"] = f"Agent error: {str(run_error)}"
            except Exception as agent_error:
                logger.error(f"Error initializing agent for {city}: {agent_error}")
                data["error"] = f"Agent initialization error: {str(agent_error)}"
                
        except Exception as e:
            logger.error(f"Unexpected error extracting appointment data for {city}: {e}")
            data["error"] = str(e)
        
        return data
    
    async def detect_changes(self, city: str, current_data: Dict) -> Dict[str, Dict]:
        """
        Detect changes in appointment data for a city.
        Returns a dictionary of changes by country.
        """
        previous_data = await self.db.get_last_appointment_data(city)
        if not previous_data:
            # For new cities, treat all available slots as changes
            changes = {}
            for country_data in current_data.get("countries", []):
                country = country_data.get("country", "")
                if not country:
                    continue
                    
                if any(slots and slots != "0" for slots in country_data.get("slots", {}).values()):
                    changes[country] = {
                        "type": "new_availability",
                        "slots": country_data.get("slots", {}),
                        "earliest_date": country_data.get("earliest_available"),
                        "url": country_data.get("url", f"{self.get_city_url(city)}/{country.lower()}")
                    }
            return changes
        
        changes = {}
        
        # Compare current and previous data
        for country_data in current_data.get("countries", []):
            country = country_data.get("country", "")
            if not country:
                continue
                
            # Find previous data for this country
            previous_country_data = next(
                (c for c in previous_data.get("countries", []) if c.get("country", "") == country),
                None
            )
            
            if not previous_country_data:
                # New country with slots
                if any(slots and slots != "0" for slots in country_data.get("slots", {}).values()):
                    changes[country] = {
                        "type": "new_country",
                        "slots": country_data.get("slots", {}),
                        "earliest_date": country_data.get("earliest_available"),
                        "url": country_data.get("url", f"{self.get_city_url(city)}/{country.lower()}")
                    }
                continue
            
            # Check for changes in slots
            current_slots = country_data.get("slots", {})
            previous_slots = previous_country_data.get("slots", {})
            
            for month, slots in current_slots.items():
                # Convert slot values to integers for comparison
                current_value = int(slots.replace("+", "")) if slots and slots != "0" else 0
                previous_value = int(previous_slots.get(month, "0").replace("+", "")) if previous_slots.get(month) else 0
                
                if current_value > previous_value:
                    changes[country] = {
                        "type": "increased_availability",
                        "month": month,
                        "previous_slots": previous_slots.get(month, "0"),
                        "new_slots": slots,
                        "earliest_date": country_data.get("earliest_available"),
                        "url": country_data.get("url", f"{self.get_city_url(city)}/{country.lower()}")
                    }
                    break
                elif current_value > 0 and previous_value == 0:
                    changes[country] = {
                        "type": "new_slots",
                        "month": month,
                        "slots": slots,
                        "earliest_date": country_data.get("earliest_available"),
                        "url": country_data.get("url", f"{self.get_city_url(city)}/{country.lower()}")
                    }
                    break
        
        return changes

    async def notify_users(self, city: str, changes: Dict[str, Dict]) -> None:
        """Notify all users monitoring a specific city about changes."""
        if not changes:
            return
        
        users = await self.db.get_users_by_city(city)
        if not users:
            return
        
        # Get the latest data to access flag information
        current_data = await self.db.get_last_appointment_data(city)
        if not current_data:
            current_data = {"countries": []}
        
        for country, change_data in changes.items():
            # Find the flag for this country
            flag_emoji = ""
            for country_data in current_data.get("countries", []):
                if country_data.get("country") == country and "flag" in country_data:
                    flag_emoji = country_data["flag"]
                    break
            
            # Add flag to change_data for use in notification message
            change_data["flag_emoji"] = flag_emoji
            
            notification_message = self._create_notification_message(city, country, change_data)
            notification_data = NotificationData(
                city=city,
                country=country,
                message=notification_message,
                change_type=change_data["type"],
                url=change_data.get("url", ""),
                timestamp=datetime.now()
            )
            
            for user in users:
                await self.notification_service.notify_user(
                    email=user.get("email"),
                    phone=user.get("phone"),
                    data=notification_data
                )
    
    def _create_notification_message(self, city: str, country: str, change_data: Dict) -> str:
        """Create a detailed notification message based on the change type."""
        # Get the flag emoji if available
        flag_emoji = change_data.get("flag_emoji", "")
                
        # Include the flag in the message if available
        country_display = f"{country} {flag_emoji}".strip()
        base_message = f"🔔 New visa appointment availability in {city} for {country_display}!\n\n"
        
        if change_data["type"] == "new_availability":
            slots_info = ", ".join(f"{month}: {slots}" for month, slots in change_data["slots"].items() if slots and slots != "0")
            message = f"{base_message}Available slots found: {slots_info}"
        
        elif change_data["type"] == "new_country":
            slots_info = ", ".join(f"{month}: {slots}" for month, slots in change_data["slots"].items() if slots and slots != "0")
            message = f"{base_message}New country added with available slots: {slots_info}"
        
        elif change_data["type"] == "increased_availability":
            message = (f"{base_message}Increased availability in {change_data['month']}!\n"
                      f"Previous: {change_data['previous_slots']} → New: {change_data['new_slots']}")
        
        elif change_data["type"] == "new_slots":
            message = f"{base_message}New slots available in {change_data['month']}: {change_data['slots']}"
        
        if change_data.get("earliest_date"):
            message += f"\nEarliest available date: {change_data['earliest_date']}"
        
        if change_data.get("url"):
            message += f"\n\nBook now: {change_data['url']}"
        
        return message

    async def monitor_appointments(self):
        """Main monitoring function."""
        # Check if monitoring is already in progress
        if self.monitoring_in_progress:
            duration_since_start = None
            if self.last_monitoring_start:
                duration_since_start = (datetime.now() - self.last_monitoring_start).total_seconds()
                # If stuck for more than 5 minutes, force reset the monitoring flag
                if duration_since_start > 300:
                    logger.warning(f"Monitoring appears stuck for {duration_since_start:.1f} seconds. Force resetting flags.")
                    self.monitoring_in_progress = False
                    await self.cleanup_browser()
                    return
                else:
                    logger.warning(
                        f"Skipping monitoring cycle - previous cycle still running "
                        f"(started {duration_since_start:.1f} seconds ago)"
                    )
                    return
            else:
                logger.warning("Skipping monitoring cycle - previous cycle still running")
                return
        
        # Set monitoring flag and timestamp
        self.monitoring_in_progress = True
        self.last_monitoring_start = datetime.now()
        logger.info("Starting appointment monitoring cycle...")
        
        all_results = []
        
        try:
            # Initialize browser once for the entire monitoring cycle
            browser_initialized = await self.initialize_browser()
            if not browser_initialized:
                logger.error("Failed to initialize browser, aborting monitoring cycle")
                self.monitoring_in_progress = False
                return
            
            # Process cities in batches to manage memory usage
            batch_size = 5  # Process 5 cities at a time
            all_cities = []
            
            # Flatten cities list
            for country, cities in self.cities_by_country.items():
                for city in cities:
                    all_cities.append((country, city))
            
            # Process cities in batches
            for i in range(0, len(all_cities), batch_size):
                batch = all_cities[i:i+batch_size]
                batch_num = i//batch_size + 1
                total_batches = (len(all_cities) + batch_size - 1) // batch_size
                logger.info(f"Processing batch {batch_num} of {total_batches}")
                
                # Process each city in the batch
                for country, city in batch:
                    logger.info(f"Checking appointments for {city}, {country}...")
                    
                    try:
                        # Extract current appointment data
                        current_data = await self.extract_appointment_data(city)
                        
                        if current_data:
                            # Save the current data to history
                            await self.db.save_appointment_data(city, current_data)
                            
                            # Only process and notify if no error
                            if "error" not in current_data:
                                # Detect changes and notify users
                                changes = await self.detect_changes(city, current_data)
                                if changes:
                                    logger.info(f"Changes detected for {city}: {json.dumps(changes, cls=MongoJSONEncoder)}")
                                    await self.notify_users(city, changes)
                        
                            all_results.append(current_data)
                    except Exception as city_error:
                        logger.error(f"Error processing {city}: {city_error}")
                        # Save minimal error data to keep history consistent
                        error_data = {"city": city, "error": str(city_error), "timestamp": datetime.now().isoformat()}
                        await self.db.save_appointment_data(city, error_data)
                    
                    # Add a small delay between cities to avoid rate limiting
                    await asyncio.sleep(2)
                
                # Reinitialize browser between batches to prevent memory issues
                await self.cleanup_browser()
                await asyncio.sleep(5)  # Brief pause
                await self.initialize_browser()
            
        except Exception as e:
            logger.error(f"Error in monitoring cycle: {e}")
        finally:
            # Ensure browser is cleaned up after the monitoring cycle
            await self.cleanup_browser()
            
            # Reset monitoring flag and update timestamps
            self.monitoring_in_progress = False
            self.last_monitoring_end = datetime.now()
            duration = (self.last_monitoring_end - self.last_monitoring_start).total_seconds()
            logger.info(f"Completed monitoring cycle in {duration:.1f} seconds")

async def run_scheduler():
    """Run the scheduler in the background."""
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

async def force_cleanup():
    """Force cleanup of system resources periodically"""
    logger.info("Running scheduled forced memory cleanup")
    try:
        # Force Python garbage collection
        gc.collect()
        
        # On Unix systems, try to free OS cache
        if os.name == 'posix':
            os.system('sync')  # Sync cached writes to persistent storage
        
        logger.info("Completed forced memory cleanup")
    except Exception as e:
        logger.error(f"Error during forced cleanup: {e}")

async def main():
    """Main function to start the monitoring process."""
    max_restarts = 3
    restart_count = 0
    restart_wait_time = 60  # seconds to wait between restarts
    
    while restart_count <= max_restarts:
        monitor = None
        try:
            # Force garbage collection before creating monitor instance
            gc.collect()
            
            # Create a new monitor instance
            monitor = AppointmentMonitor()
            
            # Run the first monitoring cycle immediately to verify everything works
            logger.info("Running initial test monitoring cycle...")
            try:
                await monitor.monitor_appointments()
            except Exception as initial_error:
                logger.critical(f"Error in initial monitoring cycle: {initial_error}")
                raise  # Re-raise to trigger restart
            
            # Schedule regular monitoring every 5 minutes
            schedule.every(5).minutes.do(lambda: asyncio.create_task(monitor.monitor_appointments()))
            
            # Add memory cleanup task every hour
            schedule.every(1).hours.do(lambda: asyncio.create_task(force_cleanup()))
            
            logger.info("Starting appointment monitoring service...")
            logger.info(f"Monitoring cities: {json.dumps(monitor.cities_by_country, indent=2)}")
            
            # Run the scheduler in the background
            await run_scheduler()
            
        except KeyboardInterrupt:
            logger.info("Received shutdown signal. Cleaning up...")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main function: {e}")
            
            restart_count += 1
            
            if restart_count <= max_restarts:
                logger.warning(f"Restarting monitoring service (attempt {restart_count}/{max_restarts}) in {restart_wait_time} seconds...")
                # Clean up resources before restart if monitor was created
                if monitor:
                    await monitor.cleanup()
                # Wait before restarting
                await asyncio.sleep(restart_wait_time)
                # Increase wait time for next restart
                restart_wait_time *= 2
            else:
                logger.critical(f"Exceeded maximum restart attempts ({max_restarts}). Service will terminate.")
                break
                
        finally:
            # Clean up resources if we're exiting the loop
            if monitor and (restart_count > max_restarts or isinstance(sys.exc_info()[1], KeyboardInterrupt)):
                await monitor.cleanup()
                logger.info("Appointment monitoring service stopped.")

if __name__ == "__main__":
    asyncio.run(main())
