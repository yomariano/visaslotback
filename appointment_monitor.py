import os
import time
import asyncio
import json
import aiohttp
from datetime import datetime
from typing import Dict, List, Optional, Any
import re
from bson import ObjectId

import google.generativeai as genai
import schedule
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from dotenv import load_dotenv
from loguru import logger

from notification_service import NotificationService, NotificationData
from mongodb import MongoDBClient

# Load environment variables
load_dotenv()

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
        
        # Initialize browser config
        self.browser_config = BrowserConfig(
            headless=True,
            verbose=True
        )
        
        # Initialize crawler config
        self.crawler_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,  # Don't cache results since we need real-time data
            js_code=[
                # Wait for dynamic content to load
                "await new Promise(r => setTimeout(r, 2000));"
            ]
        )
        
        # Get next three months for slot tracking
        current_month = datetime.now().month
        self.tracked_months = []
        for i in range(3):
            month = (current_month + i) % 12
            if month == 0:
                month = 12
            self.tracked_months.append(datetime.strptime(str(month), "%m").strftime("%b").upper())
        
        # Initialize webhook settings
        self.webhook_url = os.getenv("WEBHOOK_URL")
        self.webhook_enabled = self.webhook_url is not None and len(self.webhook_url) > 0
        
        # Initialize crawler as None - will be set up when needed
        self.crawler = None
        
        # Initialize MongoDB client and notification service
        self.db = MongoDBClient()
        self.notification_service = NotificationService()

    async def setup_crawler(self) -> bool:
        """Initialize the crawler if it hasn't been set up yet.
        
        Returns:
            bool: True if initialization was successful, False otherwise.
        """
        try:
            if self.crawler is None:
                logger.info("Initializing AsyncWebCrawler...")
                self.crawler = await AsyncWebCrawler(config=self.browser_config).__aenter__()
                logger.info("AsyncWebCrawler initialized successfully")
                return True
            return True  # Already initialized
        except Exception as e:
            logger.error(f"Error initializing crawler: {e}")
            self.crawler = None
            return False

    async def cleanup_crawler(self):
        """Clean up the crawler instance."""
        if self.crawler is not None:
            try:
                logger.info("Cleaning up AsyncWebCrawler...")
                await self.crawler.__aexit__(None, None, None)
                logger.info("AsyncWebCrawler cleaned up successfully")
            except Exception as e:
                logger.error(f"Error cleaning up crawler: {e}")
            finally:
                self.crawler = None

    async def cleanup(self):
        """Clean up all resources used by the monitor."""
        logger.info("Cleaning up AppointmentMonitor resources...")
        
        # Close the crawler if it's open
        logger.info("Closing AsyncWebCrawler...")
        await self.cleanup_crawler()
        logger.info("AsyncWebCrawler closed successfully")
        
        # Close MongoDB connection
        await self.db.close()
        logger.info("MongoDB connection closed")

    def get_city_url(self, city: str) -> str:
        """Generate the URL for a specific city's tourism appointments."""
        city_formatted = city.lower().replace(" ", "-")
        return f"{self.base_url}/in/{city_formatted}/tourism"
    
    async def extract_appointment_data(self, city: str) -> Dict:
        """Extract appointment data from the website for a specific city using Crawl4AI."""
        # Initialize basic data object for consistent returns
        data = {
            "city": city,
            "countries": [],
            "temporarily_unavailable": [],
            "timestamp": datetime.now().isoformat()
        }
        
        # Define a maximum retry count
        max_retries = 2  # Increased from 1 to 2 for better resilience
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                city_url = self.get_city_url(city)
                
                # Always verify crawler is initialized immediately before use
                crawler_valid = self.crawler is not None
                if not crawler_valid:
                    logger.warning(f"Crawler not initialized when processing {city}, initializing new instance...")
                    crawler_valid = await self.setup_crawler()
                
                if not crawler_valid:
                    # If we still don't have a valid crawler after setup attempt, report error
                    data["error"] = "Failed to initialize crawler after attempt"
                    return data
                
                # Double-check crawler is not None before proceeding
                if self.crawler is None:
                    # This is a critical error - we should NEVER reach here if crawler_valid was True
                    logger.error(f"Crawler unexpectedly None despite initialization for {city}")
                    data["error"] = "Crawler unexpectedly became None"
                    return data
                
                # Store a reference to the crawler to avoid race conditions
                current_crawler = self.crawler
                
                # Verify the crawler reference is still valid before attempting to use it
                if current_crawler is None:
                    logger.error(f"Crawler reference became None before use for {city}")
                    data["error"] = "Crawler reference became None before use"
                    return data
                
                # Use the crawler instance with proper error handling
                try:
                    # Final safety check immediately before calling arun
                    if current_crawler is None:
                        raise AttributeError("Crawler is None, cannot call arun")
                    
                    result = await current_crawler.arun(
                        url=city_url,
                        config=self.crawler_config
                    )
                except AttributeError as attr_error:
                    # Specific handling for NoneType errors
                    if "NoneType" in str(attr_error) or "None" in str(attr_error):
                        logger.critical(f"Crawler became None during operation for {city}: {attr_error}")
                        # Force cleanup and retry with a fresh crawler
                        self.crawler = None  # Ensure the reference is cleared
                        await self.cleanup_crawler()  # This is safe even if crawler is None
                        
                        # Increment retry counter
                        retry_count += 1
                        if retry_count <= max_retries:
                            logger.info(f"Attempting to create fresh crawler for {city}, retry {retry_count}/{max_retries}")
                            await asyncio.sleep(1)  # Brief pause before retry
                            crawler_valid = await self.setup_crawler()
                            if crawler_valid:
                                logger.info(f"Successfully initialized new crawler for retry {retry_count}")
                                continue
                            else:
                                logger.error(f"Failed to initialize crawler for retry {retry_count}")
                                data["error"] = f"Failed to initialize crawler after NoneType error"
                                return data
                        else:
                            logger.error(f"Exceeded retry limit after NoneType error for {city}")
                            data["error"] = f"Exceeded retry limit: {str(attr_error)}"
                            return data
                    else:
                        # Other attribute errors
                        raise
                except Exception as arun_error:
                    logger.error(f"Error during crawler.arun for {city}: {arun_error}")
                    
                    # Increment retry count
                    retry_count += 1
                    
                    # If we haven't reached max retries, create a fresh crawler instance
                    if retry_count <= max_retries:
                        logger.info(f"Creating new crawler instance and retrying (attempt {retry_count}/{max_retries})...")
                        # Completely clean up and create a new crawler instance
                        await self.cleanup_crawler()
                        if await self.setup_crawler():
                            logger.info(f"Successfully created new crawler for retry {retry_count}")
                            continue
                        else:
                            logger.error(f"Failed to create new crawler for retry {retry_count}")
                    
                    # We've exceeded retries or failed to create a new crawler
                    data["error"] = f"Crawler error: {str(arun_error)}"
                    return data
                
                # Safe access to result attributes using getattr
                success = getattr(result, 'success', None)
                if success is None:
                    logger.error(f"Invalid crawler result for {city}: missing 'success' attribute")
                    data["error"] = "Invalid crawler result (missing success attribute)"
                    return data
                    
                if success:
                    # Check if result has markdown attribute, using safe getattr
                    markdown = getattr(result, 'markdown', None)
                    raw_markdown = getattr(markdown, 'raw_markdown', None) if markdown else None
                    
                    if raw_markdown is None:
                        logger.error(f"Invalid crawler result for {city}: missing 'markdown.raw_markdown' attribute")
                        data["error"] = "Invalid crawler result (missing markdown content)"
                        return data
                    
                    content = raw_markdown
                    logger.debug(f"Raw content for {city}:\n{content}")
                    
                    # Process the content and extract data
                    try:
                        lines = content.split('\n')
                        
                        # Define known countries and their flags
                        country_indicators = {
                            'Austria': '🇦🇹',
                            'Lithuania': '🇱🇹',
                            'Netherlands': '🇳🇱',
                            'France': '🇫🇷',
                            'Germany': '🇩🇪',
                            'Italy': '🇮🇹',
                            'Spain': '🇪🇸',
                            'Switzerland': '🇨🇭',
                            'Belgium': '🇧🇪',
                            'Denmark': '🇩🇰',
                            'Finland': '🇫🇮',
                            'Greece': '🇬🇷',
                            'Iceland': '🇮🇸',
                            'Norway': '🇳🇴',
                            'Portugal': '🇵🇹',
                            'Sweden': '🇸🇪',
                            'Estonia': '🇪🇪',
                            'Hungary': '🇭🇺',
                            'Latvia': '🇱🇻',
                            'Malta': '🇲🇹',
                            'Poland': '🇵🇱',
                            'Slovenia': '🇸🇮',
                            'Croatia': '🇭🇷',
                            'Cyprus': '🇨🇾',
                            'Luxembourg': '🇱🇺',
                            'Czechia': '🇨🇿'
                        }
                        
                        current_country = None
                        current_country_data = None
                        in_table_section = False
                        in_unavailable_section = False
                        
                        for i, line in enumerate(lines):
                            line = line.strip()
                            if not line:
                                continue
                                
                            logger.debug(f"Processing line {i}: {line}")
                            
                            # Detect table header line more specifically
                            # Only check if we haven't found the table yet
                            if not in_table_section and "DESTINATION" in line.upper() and "EARLIEST" in line.upper() and "|" in line:
                                in_table_section = True
                                logger.debug(f"Table header found at line {i}")
                                continue # Skip processing the header line itself

                            # Skip lines before the table section or the separator line
                            if not in_table_section or line.startswith("---"):
                                continue

                            # Check for unavailable countries section marker
                            if "Countries below have no available slots" in line:
                                in_unavailable_section = True
                                continue

                            # Process unavailable countries
                            if in_unavailable_section and "|" in line:
                                columns = [col.strip() for col in line.split("|")]
                                country_col = columns[0].strip()
                                
                                # Check if this is a country row with "No availability"
                                if "No availability" in line:
                                    for country, flag in country_indicators.items():
                                        if country.lower() in country_col.lower() or flag in country_col:
                                            if country not in data["temporarily_unavailable"]:
                                                data["temporarily_unavailable"].append(country)
                                            break
                                continue

                            # Parse country data from table rows for available slots
                            if "|" in line and not in_unavailable_section:  # Data rows contain pipes
                                columns = [col.strip() for col in line.split("|")]
                                if len(columns) >= 5:  # Ensure we have all columns
                                    # Extract country name and flag
                                    country_col = columns[0]
                                    for country, flag in country_indicators.items():
                                        if country.lower() in country_col.lower() or flag in country_col:
                                            current_country = country
                                            current_country_data = {
                                                "country": country,
                                                "flag": flag,
                                                "earliest_available": None,
                                                "url": f"{city_url}/{country.lower()}",
                                                "slots": {month: None for month in self.tracked_months}
                                            }
                                            
                                            # Extract earliest available date
                                            date_col = columns[1]
                                            date_patterns = [
                                                r'\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)',
                                                r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}',
                                                r'\d{1,2}(?:st|nd|rd|th)\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)',
                                                r'\d{2}\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'  # Add pattern for "05 May" format
                                            ]
                                            for pattern in date_patterns:
                                                match = re.search(pattern, date_col, re.IGNORECASE)
                                                if match:
                                                    current_country_data["earliest_available"] = match.group().strip()
                                                    break
                                            
                                            # Extract slot information
                                            for idx, month in enumerate(self.tracked_months, 2):
                                                if idx < len(columns):
                                                    slot_text = columns[idx].strip()
                                                    logger.debug(f"Processing slot text for {month}: '{slot_text}'")
                                                    
                                                    # Check for slots pattern with more variations
                                                    slots_match = re.search(r'(\d+)\s*[\+]?\s*slots?|(\d+)\s*\+', slot_text, re.IGNORECASE)
                                                    if slots_match:
                                                        matched_group = slots_match.group(1) or slots_match.group(2)
                                                        slot_value = f"{matched_group}+"
                                                        current_country_data["slots"][month] = slot_value
                                                        logger.debug(f"Found slots for {month}: {slot_value}")
                                                    # Check for notify pattern
                                                    elif any(word in slot_text.lower() for word in ["notify", "notification", "alert"]):
                                                        current_country_data["slots"][month] = "0"
                                                        logger.debug(f"Found notify for {month}, setting to 0")
                                                    else:
                                                        logger.debug(f"No match found for {month} in text: '{slot_text}'")
                                            
                                            # Add to countries list if we have either date or slots
                                            if (current_country_data["earliest_available"] is not None or 
                                                any(slots for slots in current_country_data["slots"].values() if slots is not None)):
                                                data["countries"].append(current_country_data)
                                                logger.debug(f"Added country data: {current_country_data}")
                                            break
                
                        logger.info(f"Successfully extracted data from {city_url}")
                        logger.debug(f"Final parsed data: {json.dumps(data, indent=2)}")
                        return data
                    
                    except Exception as parse_error:
                        logger.error(f"Error parsing content for {city} at line {i if 'i' in locals() else 'unknown'}: {parse_error}")
                        logger.debug(f"Content causing error: {content[:500]}...")  # Show first 500 chars of content
                        data["error"] = f"Parse error: {str(parse_error)}"
                        # Don't retry parsing errors - they're not related to crawler issues
                        return data
                else:
                    # When success is False, safely get error message
                    # The 'error' attribute may not exist on all result objects
                    error_msg = "Unknown crawler error"
                    
                    # Try several possible error attribute locations
                    if hasattr(result, 'error'):
                        error_msg = str(result.error)
                    elif hasattr(result, 'error_message'):
                        error_msg = str(result.error_message)
                    elif hasattr(result, 'message'):
                        error_msg = str(result.message)
                    
                    # Log the full result object structure for debugging
                    logger.error(f"Failed to extract data for {city}: {error_msg}")
                    logger.debug(f"Full result object structure: {dir(result)}")
                    
                    data["error"] = error_msg
                    return data
                    
                # If we get here, we succeeded
                break
                    
            except Exception as e:
                logger.error(f"Error extracting appointment data for {city}: {e}")
                
                # Check if the error is related to NoneType
                if "NoneType" in str(e) or "None object has no attribute" in str(e):
                    logger.critical(f"NoneType error detected in main exception handler for {city}: {e}")
                    # Force cleanup to ensure we don't have a lingering bad state
                    self.crawler = None
                    await self.cleanup_crawler()
                
                # Increment retry count
                retry_count += 1
                
                # If we haven't reached max retries, retry
                if retry_count <= max_retries:
                    logger.info(f"Retrying extraction for {city} (attempt {retry_count}/{max_retries})...")
                    # Ensure crawler is cleaned up before next attempt
                    await self.cleanup_crawler()
                    continue
                
                # We've exceeded retries, return error data
                data["error"] = str(e)
                return data
        
        # If we get here with no data populated, ensure we return the base data
        return data

    async def send_webhook(self, data: Dict[str, Any]) -> bool:
        """Send the data to the configured webhook endpoint.
        
        Args:
            data: The data to send to the webhook
            
        Returns:
            bool: True if webhook was successfully sent, False otherwise
        """
        if not self.webhook_enabled:
            logger.warning("Webhook not configured. Set WEBHOOK_URL environment variable to enable.")
            return False

        try:
            # First validate that data can be serialized with our custom encoder
            try:
                json_data = json.dumps(data, cls=MongoJSONEncoder)
            except TypeError as json_error:
                logger.error(f"Data serialization error: {json_error}")
                logger.error(f"Problematic data type detected, attempting to fix")
                
                # Attempt to convert any problematic MongoDB ObjectId objects
                json_data = self._sanitize_for_json(data)
            
            # Send the webhook with the serialized data
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.webhook_url,
                        data=json_data,
                        headers={"Content-Type": "application/json"}
                    ) as response:
                        if response.status >= 200 and response.status < 300:
                            logger.info(f"Webhook sent successfully: {response.status}")
                            return True
                        else:
                            logger.error(f"Failed to send webhook: {response.status}")
                            response_text = await response.text()
                            logger.error(f"Response: {response_text}")
                            return False
            except aiohttp.ClientError as client_error:
                logger.error(f"HTTP client error when sending webhook: {client_error}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending webhook: {e}")
            return False
            
    def _sanitize_for_json(self, data: Any) -> str:
        """Recursively convert data to JSON-serializable format.
        
        This is a fallback for the MongoJSONEncoder to handle complex nested structures.
        
        Args:
            data: Any Python object to be serialized
            
        Returns:
            str: JSON string representation of the data
        """
        if isinstance(data, dict):
            return json.dumps({k: self._sanitize_value(v) for k, v in data.items()})
        elif isinstance(data, list):
            return json.dumps([self._sanitize_value(item) for item in data])
        else:
            return json.dumps(self._sanitize_value(data))
            
    def _sanitize_value(self, value: Any) -> Any:
        """Convert a single value to a JSON-serializable format.
        
        Args:
            value: Any Python value to be sanitized
            
        Returns:
            A JSON-serializable version of the value
        """
        if isinstance(value, ObjectId):
            return str(value)
        elif isinstance(value, datetime):
            return value.isoformat()
        elif isinstance(value, dict):
            return {k: self._sanitize_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._sanitize_value(item) for item in value]
        else:
            return value

    async def detect_changes(self, city: str, current_data: Dict) -> Dict[str, Dict]:
        """
        Detect changes in appointment data for a city.
        Returns a dictionary of changes by country.
        """
        previous_data = await self.db.get_last_appointment_data(city)
        if not previous_data:
            return {}  # No previous data to compare with
        
        changes = {}
        
        # Compare current and previous data
        for country_data in current_data.get("countries", []):
            country = country_data["country"]
            
            # Find previous data for this country
            previous_country_data = next(
                (c for c in previous_data.get("countries", []) if c["country"] == country),
                None
            )
            
            if not previous_country_data:
                # New country with slots
                if country_data["earliest_available"]:
                    changes[country] = {
                        "new_slots": "New availability",
                        "earliest_date": country_data["earliest_available"]
                    }
                continue
            
            # Check for new slots or earlier dates
            current_slots = country_data["slots"]
            previous_slots = previous_country_data["slots"]
            
            for month, slots in current_slots.items():
                if slots and slots != "0" and (
                    not previous_slots.get(month) or 
                    previous_slots[month] == "0"
                ):
                    changes[country] = {
                        "new_slots": f"{slots} slots in {month}",
                        "earliest_date": country_data["earliest_available"]
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
        
        notification_data = NotificationData(
            city=city,
            changes=changes,
            timestamp=datetime.now()
        )
        
        for user in users:
            await self.notification_service.notify_user(
                email=user.get("email"),
                phone=user.get("phone"),
                data=notification_data
            )

    async def monitor_appointments(self):
        """Main monitoring function that runs every minute."""
        logger.info("Starting appointment monitoring cycle...")
        
        all_results = []
        crawler_initialized = False
        
        try:
            # Set up the crawler at the start of the monitoring cycle
            logger.info("Initializing crawler for monitoring cycle...")
            crawler_initialized = await self.setup_crawler()
            
            if not crawler_initialized:
                logger.error("Failed to initialize crawler at start of monitoring cycle, aborting")
                return
                
            logger.info("Crawler initialized successfully, beginning city monitoring")
            
            # Monitor each city
            for country, cities in self.cities_by_country.items():
                for city in cities:
                    logger.info(f"Checking appointments for {city}, {country}...")
                    
                    try:
                        # Verify crawler is still valid before proceeding
                        if self.crawler is None:
                            logger.warning("Crawler became invalid, attempting to reinitialize...")
                            crawler_initialized = await self.setup_crawler()
                            
                            if not crawler_initialized:
                                logger.error(f"Failed to reinitialize crawler, skipping {city}")
                                error_data = {
                                    "city": city, 
                                    "error": "Failed to initialize crawler", 
                                    "timestamp": datetime.now().isoformat()
                                }
                                await self.db.save_appointment_data(city, error_data)
                                continue
                                
                            logger.info("Crawler reinitialized successfully")
                            
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
                                    logger.info(f"Changes detected for {city}: {json.dumps(changes)}")
                                    await self.notify_users(city, changes)
                            
                            all_results.append(current_data)
                    except Exception as city_error:
                        logger.error(f"Error processing {city}: {city_error}")
                        # Save minimal error data to keep history consistent
                        error_data = {"city": city, "error": str(city_error), "timestamp": datetime.now().isoformat()}
                        await self.db.save_appointment_data(city, error_data)
                        
                        # Try to recover the crawler for next city
                        try:
                            logger.info(f"Attempting to recover crawler after error in {city}")
                            await self.cleanup_crawler()
                            crawler_initialized = await self.setup_crawler()
                            if crawler_initialized:
                                logger.info("Successfully recovered crawler")
                            else:
                                logger.error("Failed to recover crawler, will retry on next city")
                        except Exception as recovery_error:
                            logger.error(f"Failed to recover crawler: {recovery_error}")
                            crawler_initialized = False
                    
                    # Add a small delay between cities to avoid rate limiting
                    await asyncio.sleep(2)
            
            # Send combined results to webhook if configured
            if self.webhook_enabled and all_results:
                try:
                    # Create a JSON-serializable copy of the results, with explicit pre-conversion of ObjectIds
                    # Process in two steps with additional safeguards for ObjectId serialization
                    logger.info("Preparing webhook payload with JSON serialization safeguards")
                    
                    # Pre-process to convert all ObjectId instances
                    sanitized_results = self._deep_sanitize_for_serialization(all_results)
                    
                    # Now safely convert to JSON
                    try:
                        json_str = json.dumps(sanitized_results, cls=MongoJSONEncoder)
                        serializable_results = json.loads(json_str)
                    except TypeError as json_error:
                        logger.error(f"JSON serialization error despite pre-sanitization: {json_error}")
                        # Fall back to our more heavy-duty sanitization approach
                        logger.warning("Using fallback sanitization for webhook payload")
                        json_str = self._sanitize_for_json(all_results)
                        serializable_results = json.loads(json_str)
                    
                    webhook_payload = {
                        "timestamp": datetime.now().isoformat(),
                        "results": serializable_results
                    }
                    
                    logger.info(f"Sending webhook with data for {len(all_results)} cities")
                    success = await self.send_webhook(webhook_payload)
                    if success:
                        logger.info("Successfully sent webhook data")
                    else:
                        logger.warning("Webhook delivery reported failure")
                except Exception as webhook_error:
                    logger.error(f"Error preparing or sending webhook payload: {webhook_error}")
                    logger.exception("Detailed webhook error")  # Log full traceback
            
        except Exception as e:
            logger.error(f"Error in monitoring cycle: {e}")
            logger.info("Attempting to reinitialize crawler on next cycle")
        finally:
            # Clean up the crawler after the monitoring cycle
            if crawler_initialized:
                try:
                    logger.info("Cleaning up crawler at end of monitoring cycle")
                    await self.cleanup_crawler()
                    logger.info("Crawler cleanup completed")
                except Exception as cleanup_error:
                    logger.error(f"Error during crawler cleanup: {cleanup_error}")
                    # Force crawler to None to ensure we don't reuse a potentially corrupted instance
                    self.crawler = None

    def _deep_sanitize_for_serialization(self, data: Any) -> Any:
        """Deep sanitize data for JSON serialization, converting all ObjectId instances to strings.
        
        This method traverses nested data structures and converts problematic types to serializable ones.
        
        Args:
            data: The data structure to sanitize (dict, list, etc.)
            
        Returns:
            The sanitized data structure with all problematic types converted to serializable ones
        """
        if isinstance(data, list):
            return [self._deep_sanitize_for_serialization(item) for item in data]
        elif isinstance(data, dict):
            return {k: self._deep_sanitize_for_serialization(v) for k, v in data.items()}
        elif isinstance(data, ObjectId):
            return str(data)
        elif isinstance(data, datetime):
            return data.isoformat()
        # Add any other problematic types here (e.g., Decimal, etc.)
        else:
            return data

async def run_scheduler():
    """Run the scheduler in the background."""
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

async def main():
    """Main function to start the monitoring process."""
    monitor = AppointmentMonitor()
    
    try:
        # Schedule monitoring every minute
        schedule.every(1).minutes.do(lambda: asyncio.create_task(monitor.monitor_appointments()))
        
        logger.info("Starting appointment monitoring service...")
        logger.info(f"Webhook enabled: {monitor.webhook_enabled}")
        logger.info(f"Monitoring cities: {json.dumps(monitor.cities_by_country, indent=2)}")
        
        # Run the scheduler in the background
        await run_scheduler()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal. Cleaning up...")
    except Exception as e:
        logger.error(f"Unexpected error in main function: {e}")
    finally:
        # Clean up resources
        await monitor.cleanup()
        logger.info("Appointment monitoring service stopped.")

if __name__ == "__main__":
    asyncio.run(main()) 