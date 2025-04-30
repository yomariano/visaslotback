import os
import time
import asyncio
import json
import aiohttp
from datetime import datetime
from typing import Dict, List, Optional, Any
import re

import google.generativeai as genai
import schedule
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from dotenv import load_dotenv
from loguru import logger

# Load environment variables
load_dotenv()

# Configure logging
logger.add(
    "logs/appointment_monitor_{time:YYYY-MM-DD}.log",
    rotation="00:00",  # Rotate at midnight
    retention="7 days",
    level="INFO"
)

class AppointmentMonitor:
    def __init__(self):
        self.base_url = "https://schengenappointments.com"
        self.cities_by_country = {
            # "Canada": ["Edmonton", "Montreal", "Ottawa", "Toronto", "Vancouver"],
            # "Ireland": ["Dublin"],
            "United Arab Emirates": ["Dubai"],
            # "United Arab Emirates": ["Abu Dhabi", "Dubai"],
            # "United Kingdom": ["Birmingham", "Cardiff", "Edinburgh", "London", "Manchester"],
            # "United States": ["Atlanta", "Boston", "Chicago", "Houston", "Los Angeles", 
            # "Miami", "New York", "San Francisco", "Seattle", "Washington DC"]
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

    def get_city_url(self, city: str) -> str:
        """Generate the URL for a specific city's tourism appointments."""
        city_formatted = city.lower().replace(" ", "-")
        return f"{self.base_url}/in/{city_formatted}/tourism"
    
    async def extract_appointment_data(self, city: str) -> Dict:
        """Extract appointment data from the website for a specific city using Crawl4AI."""
        try:
            city_url = self.get_city_url(city)
            async with AsyncWebCrawler(config=self.browser_config) as crawler:
                result = await crawler.arun(
                    url=city_url,
                    config=self.crawler_config
                )
                
                data = {
                    "city": city,
                    "countries": [],
                    "temporarily_unavailable": [],
                    "timestamp": datetime.now().isoformat()
                }
                
                if result.success:
                    content = result.markdown.raw_markdown
                    logger.debug(f"Raw content for {city}:\n{content}")
                    
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
                    
                    try:
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
                        logger.error(f"Error parsing content for {city}: {parse_error}")
                        logger.debug(f"Error occurred while processing line: {line}")
                        return {
                            "city": city,
                            "error": f"Parse error: {str(parse_error)}",
                            "raw_content": content
                        }
                else:
                    logger.error(f"Failed to extract data for {city}: {result.error}")
                    return {"city": city, "error": str(result.error)}
                    
        except Exception as e:
            logger.error(f"Error extracting appointment data for {city}: {e}")
            return {"city": city, "error": str(e)}

    async def send_webhook(self, data: Dict[str, Any]) -> bool:
        """Send the data to the configured webhook endpoint."""
        if not self.webhook_enabled:
            logger.warning("Webhook not configured. Set WEBHOOK_URL environment variable to enable.")
            return False

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.webhook_url,
                    json=data,
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
        except Exception as e:
            logger.error(f"Error sending webhook: {e}")
            return False

    async def monitor_appointments(self):
        """Main monitoring function that runs every minute."""
        logger.info("Starting appointment monitoring cycle...")
        
        all_results = []
        
        try:
            # Monitor each city
            for country, cities in self.cities_by_country.items():
                for city in cities:
                    logger.info(f"Checking appointments for {city}, {country}...")
                    
                    # Extract current appointment data
                    current_data = await self.extract_appointment_data(city)
                    
                    if current_data:
                        all_results.append(current_data)
                    
                    # Add a small delay between cities to avoid rate limiting
                    await asyncio.sleep(2)
            
            # Send combined results to webhook if configured
            if self.webhook_enabled and all_results:
                webhook_payload = {
                    "timestamp": datetime.now().isoformat(),
                    "results": all_results
                }
                await self.send_webhook(webhook_payload)
            
        except Exception as e:
            logger.error(f"Error in monitoring cycle: {e}")

async def run_scheduler():
    """Run the scheduler in the background."""
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

async def main():
    """Main function to start the monitoring process."""
    monitor = AppointmentMonitor()
    
    # Schedule monitoring every minute
    schedule.every(1).minutes.do(lambda: asyncio.create_task(monitor.monitor_appointments()))
    
    logger.info("Starting appointment monitoring service...")
    logger.info(f"Webhook enabled: {monitor.webhook_enabled}")
    logger.info(f"Monitoring cities: {json.dumps(monitor.cities_by_country, indent=2)}")
    
    # Run the scheduler in the background
    await run_scheduler()

if __name__ == "__main__":
    asyncio.run(main()) 