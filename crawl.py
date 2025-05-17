#!/usr/bin/env python3

import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
import os
import re

from playwright.async_api import async_playwright, Page, ElementHandle, TimeoutError
from mongodb import MongoDBClient
from notification_service import NotificationService, NotificationData

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class SchengenAppointmentCrawler:
    """Crawler for schengenappointments.com to extract appointment availability data."""
    
    BASE_URL = "https://schengenappointments.com/in"
    CITIES = [
        "dublin",
        "edmonton",
        "cardiff",
        "boston",
        "london",
        "manchester",
        "new-york",
        "toronto",
        "dubai"
    ]
    
    def __init__(self):
        self.browser = None
        self.context = None
        self.debug_dir = "debug_output"
        self.mongodb = MongoDBClient()
        self.notification_service = NotificationService()
        os.makedirs(self.debug_dir, exist_ok=True)
    
    async def setup(self):
        """Initialize browser and context."""
        try:
            # First check MongoDB connection health
            if not await self.mongodb.is_healthy():
                raise Exception("MongoDB connection is not healthy")
            
            # Initialize MongoDB collections and indexes
            if not await self.mongodb.initialize_collections():
                raise Exception("Failed to initialize MongoDB collections")
            
            # Initialize browser
            playwright = await async_playwright().start()
            logger.info("Starting browser...")
            self.browser = await playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox']
            )
            self.context = await self.browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            )
            logger.info("Browser setup completed successfully")
        except Exception as e:
            logger.error(f"Failed to setup: {str(e)}")
            raise
    
    async def teardown(self):
        """Close browser and context."""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            
            # Cleanup old data before closing
            deleted_count = await self.mongodb.cleanup_old_data()
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old records during teardown")
            
            await self.mongodb.close()
            logger.info("Browser and MongoDB connections closed successfully")
        except Exception as e:
            logger.error(f"Error during teardown: {str(e)}")

    async def get_city_stats(self, city: str, days: int = 30) -> Dict[str, Any]:
        """Get appointment statistics for a city."""
        try:
            stats = await self.mongodb.get_appointment_stats(city, days)
            if "error" not in stats:
                logger.info(f"Retrieved appointment stats for {city} over {days} days")
            return stats
        except Exception as e:
            logger.error(f"Error getting stats for {city}: {str(e)}")
            return {"error": str(e)}

    async def notify_users_of_changes(self, changes: List[Dict[str, Any]]) -> None:
        """
        Notify users about slot availability changes.
        """
        for change in changes:
            try:
                # Get active subscribers for this city/country combination
                active_users = await self.mongodb.get_active_subscriptions(
                    city=change["city"],
                    country=change["country"]
                )
                
                if not active_users:
                    logger.info(f"No active subscribers for {change['city']}/{change['country']}")
                    continue
                
                # Prepare notification message
                if change["change_type"] == "new_country":
                    message = (
                        f"New slots available for {change['country']} in {change['city']}!\n"
                        f"Available slots:\n"
                    )
                    for month, slots in change["current_slots"].items():
                        if slots:
                            message += f"- {month}: {slots}\n"
                else:
                    message = (
                        f"New slots available for {change['country']} in {change['city']}!\n"
                        f"Month: {change['month']}\n"
                        f"Available slots: {change['current_slots']}\n"
                    )
                
                message += f"\nBook now at: {change['url']}"
                
                # Create notification data
                notification_data = NotificationData(
                    city=change["city"],
                    country=change["country"],
                    message=message,
                    change_type=change["change_type"],
                    url=change["url"],
                    timestamp=datetime.utcnow()
                )
                
                # Notify each active user
                for user in active_users:
                    try:
                        await self.notification_service.notify_user(
                            email=user.get("email"),
                            phone=user.get("phone"),
                            data=notification_data
                        )
                        logger.info(f"Notified user {user.get('email')} about {change['city']}/{change['country']} availability")
                    except Exception as e:
                        logger.error(f"Failed to notify user {user.get('email')}: {e}")
                
            except Exception as e:
                logger.error(f"Error processing notifications for change {change}: {e}")

    async def process_city_changes(self, city: str, city_data: Dict[str, Any]) -> None:
        """
        Process changes for a city and notify users if needed.
        """
        try:
            # Add base URL to the data for generating notification URLs
            city_data["base_url"] = f"{self.BASE_URL}/{city}/tourism"
            
            # Detect changes in slot availability
            changes = await self.mongodb.detect_slot_changes(city, city_data)
            
            if changes:
                logger.info(f"Detected {len(changes)} changes for {city}")
                await self.notify_users_of_changes(changes)
            else:
                logger.info(f"No changes detected for {city}")
            
        except Exception as e:
            logger.error(f"Error processing changes for {city}: {e}")

    async def crawl_cities(self) -> Dict[str, List[Dict[str, Any]]]:
        """Main crawling function to extract data from all cities."""
        result = {"cities": []}
        
        try:
            page = await self.context.new_page()
            
            for city in self.CITIES:
                try:
                    city_url = f"{self.BASE_URL}/{city}/tourism"
                    logger.info(f"Processing city: {city} at {city_url}")
                    
                    # Navigate to the city page
                    await page.goto(city_url, wait_until='networkidle', timeout=15000)
                    await asyncio.sleep(1)  # Brief wait for dynamic content
                    
                    # Extract data for the city
                    city_data = await self.extract_city_data(page, city)
                    if city_data:
                        # Process changes and notify users before saving
                        await self.process_city_changes(city, city_data)
                        
                        # Save to MongoDB
                        saved = await self.mongodb.save_appointment_data(city, city_data)
                        if saved:
                            logger.info(f"Saved data to MongoDB for {city}")
                            result["cities"].append(city_data)
                        else:
                            logger.error(f"Failed to save data to MongoDB for {city}")
                    
                except Exception as e:
                    logger.error(f"Error processing city {city}: {str(e)}")
                    continue
            
        except Exception as e:
            logger.error(f"Error during crawling: {str(e)}")
            raise
        
        logger.info(f"Crawling completed. Found data for {len(result['cities'])} cities.")
        return result

    async def clean_text(self, text: str) -> str:
        """Clean text by removing extra whitespace and newlines."""
        if not text:
            return ""
        return re.sub(r'\s+', ' ', text).strip()

    async def extract_country_info(self, country_col: ElementHandle) -> Tuple[str, str]:
        """Extract country name and flag from a table cell."""
        try:
            # Get the link element that contains both country name and flag
            country_link = await country_col.query_selector("a")
            if not country_link:
                return "", ""
            
            # Get the link text which contains both country name and flag
            link_text = await country_link.text_content()
            link_text = await self.clean_text(link_text)
            
            # Split into country name and flag (format: "Country 🇨🇾")
            parts = link_text.split(" ")
            if len(parts) < 2:
                return link_text, ""
            
            # Last part is the flag emoji, everything before is the country name
            flag = parts[-1]
            country_name = " ".join(parts[:-1])
            
            return country_name, flag
            
        except Exception as e:
            logger.error(f"Error extracting country info: {str(e)}")
            return "", ""

    async def extract_city_data(self, page: Page, city_name: str) -> Optional[Dict[str, Any]]:
        """Extract appointment data for a specific city."""
        try:
            # Initialize city data structure
            city_data = {
                "city": city_name,
                "countries": [],
                "temporarily_unavailable": [],
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            
            # Get all rows from the tourist visa table
            try:
                # Wait for table with shorter timeout
                await page.wait_for_selector("table", timeout=10000)
                rows = await page.query_selector_all("table tbody tr")
                
                # Get month headers first
                headers = await page.query_selector_all("table thead th")
                month_names = []
                for i in range(2, 5):  # MAY, JUN, JUL columns
                    if i < len(headers):
                        header = await headers[i].text_content()
                        month_name = await self.clean_text(header)
                        month_names.append(month_name.strip().upper()[:3])
                    else:
                        month_names.append(f"M{i-1}")
                
            except TimeoutError:
                logger.warning(f"No table found for {city_name}")
                return None
            
            for row in rows:
                try:
                    # Skip rows that are just section headers
                    row_class = await row.get_attribute("class")
                    if row_class and "bg-error" in row_class:
                        continue
                    
                    # Get all columns in the row
                    cols = await row.query_selector_all("td, th")
                    
                    # Skip if row doesn't have enough columns
                    if len(cols) < 2:
                        continue
                    
                    # Get country name and flag
                    country_col = cols[0]
                    country_name, flag = await self.extract_country_info(country_col)
                    
                    if not country_name:
                        continue
                    
                    # Get earliest available date
                    earliest_col = cols[1] if len(cols) > 1 else None
                    earliest_text = await earliest_col.text_content() if earliest_col else None
                    earliest_text = await self.clean_text(earliest_text) if earliest_text else None
                    
                    # Normalize availability text
                    if earliest_text:
                        if "No availability" in earliest_text:
                            earliest_available = None
                        elif "notify me" in earliest_text.lower():
                            earliest_available = "🔔 Notify me"
                        else:
                            # Extract just the date part
                            date_match = re.search(r'\d{1,2}\s+\w{3}', earliest_text)
                            earliest_available = date_match.group(0) if date_match else earliest_text
                    else:
                        earliest_available = None
                    
                    # Initialize consistent slot structure
                    slots = {month: None for month in month_names}
                    
                    # Get slot counts for next 3 months
                    if len(cols) > 4:  # If we have month columns
                        month_cols = cols[2:5]  # Next 3 months columns
                        
                        # Extract slot counts
                        for i, col in enumerate(month_cols):
                            month_text = await col.text_content()
                            month_text = await self.clean_text(month_text)
                            month_name = month_names[i]
                            
                            if month_text and not any(x in month_text.lower() for x in ["no availability", "notify"]):
                                slots[month_name] = month_text.strip()
                    
                    # Add country data if we have either availability or slots
                    if earliest_available or any(v is not None for v in slots.values()):
                        country_data = {
                            "country": country_name,
                            "flag": flag,
                            "earliest_available": earliest_available,
                            "slots": slots
                        }
                        city_data["countries"].append(country_data)
                    
                except Exception as e:
                    logger.error(f"Error processing row in {city_name}: {str(e)}")
                    continue
            
            # Try to extract temporarily unavailable countries
            try:
                unavailable_section = await page.query_selector("div.alert-warning")
                if unavailable_section:
                    unavailable_text = await unavailable_section.text_content()
                    if "Temporarily unavailable:" in unavailable_text:
                        countries = unavailable_text.split(":")[-1].strip()
                        if countries:
                            city_data["temporarily_unavailable"] = [
                                await self.clean_text(c) 
                                for c in countries.split(",")
                            ]
            except Exception as e:
                logger.debug(f"No temporarily unavailable countries found for {city_name}: {str(e)}")
            
            return city_data
            
        except Exception as e:
            logger.error(f"Error extracting data for {city_name}: {str(e)}")
            return None

async def main():
    """Main entry point for the crawler."""
    crawler = SchengenAppointmentCrawler()
    try:
        await crawler.setup()
        result = await crawler.crawl_cities()
        
        # Get stats for each city after crawling
        for city in crawler.CITIES:
            try:
                stats = await crawler.get_city_stats(city)
                if "error" not in stats:
                    logger.info(f"Stats for {city}:")
                    logger.info(json.dumps(stats, indent=2))
            except Exception as e:
                logger.error(f"Error getting stats for {city}: {str(e)}")
    finally:
        await crawler.teardown()

if __name__ == "__main__":
    asyncio.run(main()) 