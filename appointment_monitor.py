import os
import time
import asyncio
import json
import aiohttp
from datetime import datetime
from typing import Dict, List, Optional, Any

import google.generativeai as genai
import schedule
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from dotenv import load_dotenv
from loguru import logger

# Load environment variables
load_dotenv()

# Configure logging
logger.add(
    "logs/appointment_monitor_{date}.log",
    rotation="00:00",  # Rotate at midnight
    retention="7 days",
    level="INFO"
)

class AppointmentMonitor:
    def __init__(self):
        self.target_url = "https://schengenappointments.com/"
        
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
        
        # Initialize Gemini
        genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
        self.model = genai.GenerativeModel("gemini-2.0-flash-exp")
        self.max_retries = 3
        self.retry_delay = 20  # seconds
        
        # Initialize state
        self.previous_state: Optional[Dict] = None
        
        # Initialize webhook settings
        self.webhook_url = os.getenv("WEBHOOK_URL")
        self.webhook_enabled = self.webhook_url is not None and len(self.webhook_url) > 0
    
    async def extract_appointment_data(self) -> Dict:
        """Extract appointment data from the website using Crawl4AI."""
        try:
            async with AsyncWebCrawler(config=self.browser_config) as crawler:
                result = await crawler.arun(
                    url=self.target_url,
                    config=self.crawler_config
                )
                
                # Extract data from the result
                data = {
                    "available_slots": [],
                    "locations": []
                }
                
                if result.success:
                    # Parse the markdown content for appointment data
                    # This assumes the appointment slots and locations are in the page content
                    content = result.markdown.raw_markdown
                    
                    # TODO: Parse the content to extract slots and locations
                    # This will need to be adjusted based on the actual website structure
                    
                    logger.info(f"Successfully extracted data from {self.target_url}")
                    return data
                else:
                    logger.error(f"Failed to extract data: {result.error}")
                    return {}
                    
        except Exception as e:
            logger.error(f"Error extracting appointment data: {e}")
            return {}

    async def analyze_changes(self, current_data: Dict) -> Optional[Dict[str, Any]]:
        """Use Gemini to analyze changes in appointment availability and return structured data."""
        if not self.previous_state:
            self.previous_state = current_data
            return None
        
        # Prepare context for Gemini
        context = {
            "previous_state": self.previous_state,
            "current_state": current_data,
            "timestamp": datetime.now().isoformat()
        }
        
        prompt = f"""
        Analyze the following Schengen visa appointment data and identify significant changes:
        
        Previous state: {context['previous_state']}
        Current state: {context['current_state']}
        Timestamp: {context['timestamp']}
        
        Please provide a JSON object with the following structure:
        {{
          "new_slots": [list of new appointment slots],
          "removed_slots": [list of removed appointment slots],
          "location_changes": [changes in location availability],
          "other_changes": [any other notable changes],
          "summary": "A brief text summary of the changes"
        }}
        
        Return ONLY the JSON object without any additional text or formatting. Do not wrap the JSON in markdown code blocks.
        """
        
        for attempt in range(self.max_retries):
            try:
                response = await self.model.generate_content_async(prompt)
                # Fix: Properly handle the asynchronous response
                if response and hasattr(response, 'text'):
                    try:
                        # Extract JSON from potential markdown code blocks
                        text = response.text.strip()
                        
                        # Check if the response is wrapped in markdown code block
                        if text.startswith("```json") or text.startswith("```"):
                            # Remove the opening code block marker
                            if text.startswith("```json"):
                                text = text[7:]  # Remove ```json
                            elif text.startswith("```"):
                                text = text[3:]  # Remove ```
                                
                            # Remove the closing code block marker
                            if text.endswith("```"):
                                text = text[:-3]  # Remove trailing ```
                                
                            # Trim whitespace
                            text = text.strip()
                            
                        logger.debug(f"Cleaned JSON text: {text}")
                        
                        # Parse the response as JSON
                        analysis_json = json.loads(text)
                        # Add timestamp
                        analysis_json["timestamp"] = datetime.now().isoformat()
                        return analysis_json
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse Gemini response as JSON: {e}")
                        logger.debug(f"Raw response: {response.text}")
                        # Try to create a basic JSON response with the raw text
                        return {
                            "timestamp": datetime.now().isoformat(),
                            "error": "Could not parse analysis as JSON",
                            "raw_analysis": response.text
                        }
                else:
                    logger.error("Invalid response format from Gemini API")
                    return None
            except Exception as e:
                if "429" in str(e) and attempt < self.max_retries - 1:  # Rate limit error
                    logger.warning(f"Rate limit hit, retrying in {self.retry_delay} seconds (attempt {attempt + 1}/{self.max_retries})")
                    await asyncio.sleep(self.retry_delay)
                    continue
                logger.error(f"Error analyzing changes with Gemini: {e}")
                return None

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
        
        try:
            # Extract current appointment data
            current_data = await self.extract_appointment_data()
            
            if current_data:
                # Analyze changes using Gemini as structured JSON
                analysis = await self.analyze_changes(current_data)
                
                if analysis:
                    logger.info(f"Change Analysis:\n{json.dumps(analysis, indent=2)}")
                    
                    # Send data to webhook if configured
                    if self.webhook_enabled:
                        webhook_payload = {
                            "timestamp": datetime.now().isoformat(),
                            "current_data": current_data,
                            "previous_data": self.previous_state,
                            "analysis": analysis
                        }
                        await self.send_webhook(webhook_payload)
                
                # Update previous state
                self.previous_state = current_data
            
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
    
    # Run the scheduler in the background
    await run_scheduler()

if __name__ == "__main__":
    asyncio.run(main()) 