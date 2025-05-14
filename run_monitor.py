#!/usr/bin/env python3
"""
Run script for the appointment monitor using browser_use agent.
"""

import os
import sys
import asyncio
from dotenv import load_dotenv
from loguru import logger

from appointment_monitor import main

# Load environment variables
load_dotenv()

# Configure logging
logger.add(
    "logs/run_monitor_{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention="7 days",
    level="INFO"
)

if __name__ == "__main__":
    logger.info("Starting appointment monitor service")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service stopped by user")
    except Exception as e:
        logger.error(f"Service crashed: {e}")
        sys.exit(1) 