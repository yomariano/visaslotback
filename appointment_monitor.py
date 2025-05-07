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
        
        # Initialize browser config with memory management options
        self.browser_config = BrowserConfig(
            headless=True,
            verbose=True,
            args=[
                '--disable-dev-shm-usage',  # Prevent memory issues in containers
                '--no-sandbox',  # Required for running as non-root
                '--disable-gpu',  # Reduce memory usage
                '--disable-software-rasterizer',  # Reduce memory usage
                '--disable-extensions',  # Disable unnecessary features
                '--single-process',  # Use single process to prevent orphaned processes
                '--memory-pressure-off',  # Prevent aggressive memory management
                '--js-flags=--max-old-space-size=512'  # Limit JS heap size
            ]
        )
        
        # Initialize crawler config with timeout
        self.crawler_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,  # Don't cache results since we need real-time data
            js_code=[
                # Wait for dynamic content to load with timeout
                "await Promise.race([new Promise(r => setTimeout(r, 2000)), new Promise(r => setTimeout(() => r('timeout'), 10000))]);"
            ],
            timeout=15000  # 15 second timeout for operations
        )
        
        # Get next three months for slot tracking
        current_month = datetime.now().month
        self.tracked_months = []
        for i in range(3):
            month = (current_month + i) % 12
            if month == 0:
                month = 12
            self.tracked_months.append(datetime.strptime(str(month), "%m").strftime("%b").upper())
        
        # Initialize crawler as None - will be set up when needed
        self.crawler = None
        self._initializing_crawler = False
        self._last_crawler_cleanup = None
        
        # Initialize MongoDB client and notification service
        self.db = MongoDBClient()
        self.notification_service = NotificationService()
        
        # Add monitoring flag to prevent concurrent runs
        self.monitoring_in_progress = False
        self.last_monitoring_start = None
        self.last_monitoring_end = None

    async def setup_crawler(self) -> bool:
        """Initialize the crawler if it hasn't been set up yet."""
        try:
            # Force cleanup if it's been more than 5 minutes since last cleanup
            current_time = datetime.now()
            if (self._last_crawler_cleanup is None or 
                (current_time - self._last_crawler_cleanup).total_seconds() > 300):
                await self.cleanup_crawler()
                self._last_crawler_cleanup = current_time

            # Check if crawler already exists and is valid
            if self.crawler is not None:
                return True

            # Use a flag to prevent recursive calls
            if self._initializing_crawler:
                logger.warning("Avoiding recursive setup_crawler call")
                return False
                
            # Set flag to indicate we're initializing
            self._initializing_crawler = True
            
            # Create a new crawler instance with timeout
            try:
                self.crawler = await asyncio.wait_for(
                    AsyncWebCrawler(config=self.browser_config).__aenter__(),
                    timeout=30  # 30 second timeout for initialization
                )
            except asyncio.TimeoutError:
                logger.error("Crawler initialization timed out")
                self.crawler = None
                self._initializing_crawler = False
                return False
            
            # Reset the initialization flag
            self._initializing_crawler = False
            
            return True
        except Exception as e:
            logger.error(f"Error initializing crawler: {e}")
            # Ensure crawler is set to None on failure
            self.crawler = None
            # Reset the initialization flag
            self._initializing_crawler = False
            return False

    async def cleanup_crawler(self):
        """Clean up the crawler instance and ensure Chrome processes are terminated."""
        if self.crawler is not None:
            try:
                # Set a timeout for the cleanup operation
                await asyncio.wait_for(
                    self.crawler.__aexit__(None, None, None),
                    timeout=10  # 10 second timeout for cleanup
                )
            except asyncio.TimeoutError:
                logger.error("Crawler cleanup timed out")
            except Exception as e:
                logger.error(f"Error cleaning up crawler: {e}")
            finally:
                self.crawler = None
                self._initializing_crawler = False
                
                # Force garbage collection to free memory
                import gc
                gc.collect()
                
                # Update last cleanup timestamp
                self._last_crawler_cleanup = datetime.now()

    async def cleanup(self):
        """Clean up all resources used by the monitor."""
        
        # Close the crawler if it's open
        await self.cleanup_crawler()
        
        # Close MongoDB connection
        await self.db.close()

    def get_city_url(self, city: str) -> str:
        """Generate the URL for a specific city's tourism appointments."""
        city_formatted = city.lower().replace(" ", "-")
        url = f"{self.base_url}/in/{city_formatted}/tourism"
        return url.replace("...", "")  # Remove any ellipsis that might have been added
    
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
                    
                    # Reset initialization flag to avoid recursion
                    self._initializing_crawler = False
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
                            
                            # Reset initialization flag to avoid recursion
                            self._initializing_crawler = False
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
                        
                        # Reset initialization flag to avoid recursion
                        self._initializing_crawler = False
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
                                
                            # Detect table header line more specifically
                            # Only check if we haven't found the table yet
                            if not in_table_section and "DESTINATION" in line.upper() and "EARLIEST" in line.upper() and "|" in line:
                                in_table_section = True
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
                                                logger.info(f"Added {country} to temporarily unavailable list for {city}")
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
                                                    
                                                    # Check for slots pattern with more variations
                                                    slots_match = re.search(r'(\d+)\s*[\+]?\s*slots?|(\d+)\s*\+', slot_text, re.IGNORECASE)
                                                    if slots_match:
                                                        matched_group = slots_match.group(1) or slots_match.group(2)
                                                        slot_value = f"{matched_group}+"
                                                        current_country_data["slots"][month] = slot_value
                                                    # Check for notify pattern
                                                    elif any(word in slot_text.lower() for word in ["notify", "notification", "alert"]):
                                                        current_country_data["slots"][month] = "0"
                                                    
                                            # Add to countries list if we have either date or slots
                                            if (current_country_data["earliest_available"] is not None or 
                                                any(slots for slots in current_country_data["slots"].values() if slots is not None)):
                                                data["countries"].append(current_country_data)
                                                logger.info(f"Found available slots for {country} in {city}")
                                            break
                
                        logger.info(f"Successfully extracted data from {city_url}")
                        return data
                    
                    except Exception as parse_error:
                        logger.error(f"Error parsing content for {city} at line {i if 'i' in locals() else 'unknown'}: {parse_error}")
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
                country = country_data["country"]
                if any(slots and slots != "0" for slots in country_data["slots"].values()):
                    changes[country] = {
                        "type": "new_availability",
                        "slots": country_data["slots"],
                        "earliest_date": country_data["earliest_available"],
                        "url": country_data["url"]
                    }
            return changes
        
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
                if any(slots and slots != "0" for slots in country_data["slots"].values()):
                    changes[country] = {
                        "type": "new_country",
                        "slots": country_data["slots"],
                        "earliest_date": country_data["earliest_available"],
                        "url": country_data["url"]
                    }
                continue
            
            # Check for changes in slots
            current_slots = country_data["slots"]
            previous_slots = previous_country_data["slots"]
            
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
                        "earliest_date": country_data["earliest_available"],
                        "url": country_data["url"]
                    }
                    break
                elif current_value > 0 and previous_value == 0:
                    changes[country] = {
                        "type": "new_slots",
                        "month": month,
                        "slots": slots,
                        "earliest_date": country_data["earliest_available"],
                        "url": country_data["url"]
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
        
        for country, change_data in changes.items():
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
        base_message = f"🔔 New visa appointment availability in {city} for {country}!\n\n"
        
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
        """Main monitoring function that runs every minute."""
        # Check if monitoring is already in progress
        if self.monitoring_in_progress:
            duration_since_start = None
            if self.last_monitoring_start:
                duration_since_start = (datetime.now() - self.last_monitoring_start).total_seconds()
                # Force cleanup if monitoring has been running too long (15 minutes)
                if duration_since_start > 900:  # 15 minutes
                    logger.warning(f"Forcing cleanup of stale monitoring cycle after {duration_since_start:.1f} seconds")
                    self.monitoring_in_progress = False
                    await self.cleanup_crawler()
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
        crawler_initialized = False
        cities_processed = 0
        
        try:
            # Ensure we don't have a stale crawler instance
            await self.cleanup_crawler()
                
            # Reset initialization flag to avoid stale state
            self._initializing_crawler = False
            
            # Set up the crawler at the start of the monitoring cycle
            crawler_initialized = await self.setup_crawler()
            
            if not crawler_initialized:
                logger.error("Failed to initialize crawler at start of monitoring cycle, aborting")
                return
                
            # Monitor each city
            total_cities = sum(len(cities) for cities in self.cities_by_country.values())
            
            for country, cities in self.cities_by_country.items():
                for city in cities:
                    cities_processed += 1
                    logger.info(f"Checking appointments for {city}, {country} ({cities_processed}/{total_cities})...")
                    
                    try:
                        # Recreate crawler every 5 cities to prevent memory buildup
                        if cities_processed % 5 == 0:
                            logger.info("Performing routine crawler cleanup and recreation")
                            await self.cleanup_crawler()
                            crawler_initialized = await self.setup_crawler()
                            if not crawler_initialized:
                                logger.error(f"Failed to reinitialize crawler during routine cleanup, skipping remaining cities")
                                break
                        
                        # Verify crawler is still valid before proceeding
                        if self.crawler is None:
                            logger.warning("Crawler became invalid, attempting to reinitialize...")
                            
                            # Reset the initialization flag before trying again
                            self._initializing_crawler = False
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
                            
                        # Force garbage collection after processing each city
                        import gc
                        gc.collect()
                        
                    except Exception as city_error:
                        logger.error(f"Error processing {city}: {city_error}")
                        # Save minimal error data to keep history consistent
                        error_data = {"city": city, "error": str(city_error), "timestamp": datetime.now().isoformat()}
                        await self.db.save_appointment_data(city, error_data)
                        
                        # Try to recover the crawler for next city
                        try:
                            await self.cleanup_crawler()
                            # Reset initialization flag
                            self._initializing_crawler = False
                            crawler_initialized = await self.setup_crawler()
                            if not crawler_initialized:
                                logger.error("Failed to recover crawler, will retry on next city")
                        except Exception as recovery_error:
                            logger.error(f"Failed to recover crawler: {recovery_error}")
                            crawler_initialized = False
                    
                    # Add a small delay between cities to avoid rate limiting
                    await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Error in monitoring cycle: {e}")
        finally:
            # Clean up the crawler after the monitoring cycle
            if self.crawler is not None:
                try:
                    await self.cleanup_crawler()
                except Exception as cleanup_error:
                    logger.error(f"Error during crawler cleanup: {cleanup_error}")
                    self.crawler = None
                    self._initializing_crawler = False
            
            # Reset monitoring flag and update timestamps
            self.monitoring_in_progress = False
            self.last_monitoring_end = datetime.now()
            duration = (self.last_monitoring_end - self.last_monitoring_start).total_seconds()
            logger.info(f"Completed monitoring cycle in {duration:.1f} seconds. Processed {cities_processed} cities.")

async def run_scheduler():
    """Run the scheduler in the background."""
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

async def main():
    """Main function to start the monitoring process."""
    max_restarts = 3
    restart_count = 0
    restart_wait_time = 60  # seconds to wait between restarts
    
    while restart_count <= max_restarts:
        monitor = None
        try:
            # Create a new monitor instance
            monitor = AppointmentMonitor()
            
            # Run the first monitoring cycle immediately to verify everything works
            logger.info("Running initial test monitoring cycle...")
            await monitor.monitor_appointments()
            
            # Schedule regular monitoring every minute
            schedule.every(1).minutes.do(lambda: asyncio.create_task(monitor.monitor_appointments()))
            
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
    import sys
    asyncio.run(main()) 