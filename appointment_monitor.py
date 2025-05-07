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
        
        # Define memory optimization arguments for Chrome
        self.browser_optimization_args = [
            "--disable-dev-shm-usage",  # Overcome limited /dev/shm in containers
            "--disable-gpu",            # Disable GPU hardware acceleration
            "--disable-extensions",     # Disable extensions to reduce memory
            "--no-sandbox",             # Required for some environments
            "--disable-setuid-sandbox", # Additional sandbox disabling
            "--no-zygote",              # Don't fork zygote processes
            "--disable-infobars",       # Don't show infobars
            "--disable-features=TranslateUI,BlinkGenPropertyTrees",
            "--disable-translate",      # Disable translate
            "--blink-settings=imagesEnabled=false", # Disable images for memory saving
            "--disable-dev-tools",      # Disable dev tools
            "--mute-audio",             # Mute audio
            "--memory-pressure-off",    # Turn off memory pressure signal
            "--js-flags=--max_old_space_size=256" # Limit JS memory heap size
        ]
        
        # Initialize browser config - only use the params we know are supported
        self.browser_config = BrowserConfig(
            headless=True,
            verbose=True
        )
        
        # Initialize crawler config
        # Check if browser_args is supported in the signature
        try:
            from inspect import signature
            run_sig = signature(CrawlerRunConfig.__init__)
            browser_args_supported = "browser_args" in run_sig.parameters
            
            # Create configuration with or without browser_args
            if browser_args_supported:
                self.crawler_config = CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    js_code=["await new Promise(r => setTimeout(r, 2000));"],
                    browser_args=self.browser_optimization_args
                )
                logger.info("Created crawler config with browser_args parameter")
            else:
                self.crawler_config = CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    js_code=["await new Promise(r => setTimeout(r, 2000));"]
                )
                # Try to set browser_args as an attribute if the parameter isn't supported
                # but the attribute might be
                try:
                    self.crawler_config.browser_args = self.browser_optimization_args
                    logger.info("Set browser_args as attribute on crawler config")
                except Exception as attr_err:
                    logger.warning(f"Could not set browser_args attribute: {attr_err}")
                    logger.warning("Browser memory optimization will be limited")
        
        except Exception as config_err:
            logger.error(f"Error determining CrawlerRunConfig parameters: {config_err}")
            # Fallback to basic configuration
            self.crawler_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                js_code=["await new Promise(r => setTimeout(r, 2000));"]
            )
            logger.warning("Using fallback crawler configuration without memory optimizations")
        
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
        
        # Initialize MongoDB client and notification service
        self.db = MongoDBClient()
        self.notification_service = NotificationService()
        
        # Add monitoring flag to prevent concurrent runs
        self.monitoring_in_progress = False
        self.last_monitoring_start = None
        self.last_monitoring_end = None

    async def setup_crawler(self) -> bool:
        """Initialize the crawler if it hasn't been set up yet.
        
        Returns:
            bool: True if initialization was successful, False otherwise.
        """
        try:
            # Check if crawler already exists and is valid
            if self.crawler is not None:
                return True

            # Use a flag to prevent recursive calls
            if self._initializing_crawler:
                logger.warning("Avoiding recursive setup_crawler call")
                return False
                
            # Set flag to indicate we're initializing
            self._initializing_crawler = True
            
            # Force garbage collection before starting new crawler
            try:
                import gc
                gc.collect()
            except Exception as gc_error:
                logger.warning(f"Error during pre-initialization garbage collection: {gc_error}")
            
            # Create a new crawler instance with explicit timeout
            try:
                # Log the browser configuration we're using
                logger.info("Initializing crawler with memory optimization")
                
                self.crawler = await asyncio.wait_for(
                    AsyncWebCrawler(config=self.browser_config).__aenter__(),
                    timeout=30  # 30 second timeout for initialization
                )
                
                # Verify the crawler has necessary methods and is properly initialized
                if not hasattr(self.crawler, 'arun'):
                    logger.error("Crawler is missing required 'arun' method")
                    self.crawler = None
                    self._initializing_crawler = False
                    return False
                    
                logger.info("Successfully initialized crawler with memory optimization")
                
            except asyncio.TimeoutError:
                logger.error("Timeout while initializing crawler")
                self._initializing_crawler = False
                self.crawler = None
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
        """Clean up the crawler instance with aggressive memory cleanup."""
        if self.crawler is not None:
            try:
                # Attempt normal cleanup
                await self.crawler.__aexit__(None, None, None)
            except Exception as e:
                logger.error(f"Error during standard crawler cleanup: {e}")
                try:
                    # Try direct browser closing if available
                    if hasattr(self.crawler, 'browser') and self.crawler.browser:
                        await self.crawler.browser.close()
                except Exception as be:
                    logger.error(f"Error closing browser directly: {be}")
            finally:
                # Force garbage collection after browser cleanup
                try:
                    import gc
                    gc.collect()
                except Exception as gc_error:
                    logger.error(f"Error during forced garbage collection: {gc_error}")
                
                # Reset crawler reference
                self.crawler = None
                self._initializing_crawler = False

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
                    
                    # Ensure we're using the config with browser optimizations
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
                # If stuck for more than 5 minutes, force reset the monitoring flag
                if duration_since_start > 300:
                    logger.warning(f"Monitoring appears stuck for {duration_since_start:.1f} seconds. Force resetting flags.")
                    self.monitoring_in_progress = False
                    # Force cleanup of any browser resources
                    await self.cleanup_crawler()
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
                logger.info(f"Processing batch {i//batch_size + 1} of {(len(all_cities) + batch_size - 1) // batch_size}")
                
                # Create a fresh crawler for each batch
                await self.cleanup_crawler()
                
                # Reset initialization flag
                self._initializing_crawler = False
                crawler_initialized = await self.setup_crawler()
                
                if not crawler_initialized:
                    logger.error(f"Failed to initialize crawler for batch {i//batch_size + 1}, skipping batch")
                    # Sleep briefly before attempting next batch
                    await asyncio.sleep(5)
                    continue
                
                # Process each city in the batch
                for country, city in batch:
                    logger.info(f"Checking appointments for {city}, {country}...")
                    
                    try:
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
                
                # Force garbage collection after each batch
                logger.info(f"Completed batch {i//batch_size + 1}, performing cleanup")
                await self.cleanup_crawler()
                import gc
                gc.collect()
                
                # Sleep briefly between batches to let system resources recover
                await asyncio.sleep(5)
            
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
            logger.info(f"Completed monitoring cycle in {duration:.1f} seconds")

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
            
            # Schedule regular monitoring at a reduced frequency to prevent resource buildup
            # Changed from every 1 minute to every 5 minutes
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
            
            # Check if the error is related to configuration
            if "BrowserConfig" in str(e) or "CrawlerRunConfig" in str(e):
                logger.critical(f"Critical configuration error detected: {e}")
                logger.critical("This appears to be an issue with the crawl4ai library configuration.")
                logger.critical("Please check the crawl4ai library documentation for correct configuration parameters.")
                
                # If this is a recurring configuration error, we should break to avoid constant restarts
                if restart_count > 0:
                    logger.critical("Multiple configuration errors detected. Stopping service.")
                    break
            
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

# Add new force cleanup function
async def force_cleanup():
    """Force cleanup of system resources periodically"""
    logger.info("Running scheduled forced memory cleanup")
    try:
        # Force Python garbage collection
        import gc
        gc.collect()
        
        # On Unix systems, try to free OS cache
        if os.name == 'posix':
            os.system('sync')  # Sync cached writes to persistent storage
        
        logger.info("Completed forced memory cleanup")
    except Exception as e:
        logger.error(f"Error during forced cleanup: {e}")

async def diagnose_crawler_config():
    """Diagnostic function to inspect the crawl4ai configuration options.
    This is intended for debugging configuration issues.
    """
    try:
        from inspect import signature, getmro
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        
        # Inspect BrowserConfig
        logger.info("Examining BrowserConfig class:")
        browser_sig = signature(BrowserConfig.__init__)
        logger.info(f"BrowserConfig constructor signature: {browser_sig}")
        
        # Inspect BrowserConfig hierarchy
        browser_mro = getmro(BrowserConfig)
        logger.info(f"BrowserConfig class hierarchy: {[cls.__name__ for cls in browser_mro]}")
        
        # Inspect CrawlerRunConfig
        logger.info("Examining CrawlerRunConfig class:")
        run_sig = signature(CrawlerRunConfig.__init__)
        logger.info(f"CrawlerRunConfig constructor signature: {run_sig}")
        
        # Try creating simple instances
        logger.info("Attempting to create test instances:")
        browser_config = BrowserConfig(headless=True)
        logger.info(f"Successfully created BrowserConfig: {browser_config}")
        
        run_config = CrawlerRunConfig()
        logger.info(f"Successfully created CrawlerRunConfig: {run_config}")
        
        # Report success
        logger.info("Diagnostic complete - configuration classes appear accessible")
        return True
        
    except Exception as e:
        logger.error(f"Error during configuration diagnosis: {e}")
        return False

# Add hook to run diagnostic before main if needed
if __name__ == "__main__":
    import sys
    
    # Check for diagnostic mode
    if len(sys.argv) > 1 and sys.argv[1] == "--diagnose":
        asyncio.run(diagnose_crawler_config())
    else:
        # Normal execution
        asyncio.run(main()) 