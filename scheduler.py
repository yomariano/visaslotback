#!/usr/bin/env python3

import asyncio
import signal
import sys
import time
from datetime import datetime
import schedule
from loguru import logger
import threading
import os

from crawl import SchengenAppointmentCrawler

# Configure logging
os.makedirs("logs", exist_ok=True)
logger.add(
    "logs/scheduler_{time:YYYY-MM-DD}.log",
    rotation="00:00",  # Rotate at midnight
    retention="7 days",
    level="INFO"
)

class CrawlerScheduler:
    def __init__(self):
        self.is_running = False
        self.current_task = None
        self.crawler = None
        self.loop = None
        
    async def initialize_crawler(self):
        """Initialize the crawler if not already initialized."""
        try:
            if self.crawler is None:
                self.crawler = SchengenAppointmentCrawler()
                await self.crawler.setup()
                logger.info("Crawler initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize crawler: {e}")
            raise

    async def cleanup_crawler(self):
        """Cleanup crawler resources."""
        try:
            if self.crawler:
                await self.crawler.teardown()
                self.crawler = None
                logger.info("Crawler cleaned up successfully")
        except Exception as e:
            logger.error(f"Error during crawler cleanup: {e}")

    async def run_crawler_task(self):
        """Run a single crawler iteration."""
        try:
            if not self.crawler:
                await self.initialize_crawler()
            
            logger.info("Starting crawler iteration")
            start_time = time.time()
            
            await self.crawler.crawl_cities()
            
            duration = time.time() - start_time
            logger.info(f"Completed crawler iteration in {duration:.2f} seconds")
            
        except Exception as e:
            logger.error(f"Error during crawler iteration: {e}")
            # Cleanup and reinitialize on error
            await self.cleanup_crawler()
        finally:
            # Brief pause to prevent immediate restart
            await asyncio.sleep(1)

    def run_crawler_sync(self):
        """Synchronous wrapper for the crawler task."""
        try:
            if self.current_task and not self.current_task.done():
                logger.warning("Previous crawler task still running, skipping this iteration")
                return

            if self.loop is None or self.loop.is_closed():
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)

            # Run the crawler task
            self.current_task = self.loop.create_task(self.run_crawler_task())
            self.loop.run_until_complete(self.current_task)

        except Exception as e:
            logger.error(f"Error in crawler sync wrapper: {e}")
            # Reset task on error
            self.current_task = None
            
            # Cleanup loop on error
            if self.loop and not self.loop.is_closed():
                self.loop.close()
                self.loop = None

    def schedule_runner(self):
        """Run scheduled tasks in a separate thread."""
        while self.is_running:
            schedule.run_pending()
            time.sleep(1)

    def start(self):
        """Start the scheduler."""
        try:
            self.is_running = True
            
            # Schedule the job to run every minute
            schedule.every(1).minutes.do(self.run_crawler_sync)
            
            # Run immediately on start
            self.run_crawler_sync()
            
            logger.info("Scheduler started")
            
            # Start scheduler in a separate thread
            scheduler_thread = threading.Thread(target=self.schedule_runner)
            scheduler_thread.daemon = True
            scheduler_thread.start()
            
            # Keep main thread alive
            while self.is_running:
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"Error in scheduler: {e}")
            self.is_running = False
            raise

    def stop(self):
        """Stop the scheduler and cleanup resources."""
        self.is_running = False
        
        if self.loop and not self.loop.is_closed():
            try:
                # Cancel current task if running
                if self.current_task and not self.current_task.done():
                    self.current_task.cancel()
                    self.loop.run_until_complete(self.current_task)
                
                # Run cleanup
                self.loop.run_until_complete(self.cleanup_crawler())
                
                # Close the loop
                self.loop.close()
            except Exception as e:
                logger.error(f"Error during scheduler stop: {e}")
            finally:
                self.loop = None
                self.current_task = None
        
        logger.info("Scheduler stopped")

def signal_handler(signum, frame):
    """Handle termination signals."""
    logger.info(f"Received signal {signum}")
    
    try:
        scheduler.stop()
    except Exception as e:
        logger.error(f"Error during signal handling: {e}")
    finally:
        sys.exit(0)

if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create and start the scheduler
    scheduler = CrawlerScheduler()
    
    try:
        scheduler.start()
    except Exception as e:
        logger.error(f"Scheduler error: {e}")
        scheduler.stop()
        sys.exit(1) 