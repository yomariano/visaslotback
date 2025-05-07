#!/usr/bin/env python3
"""
Test script for crawl4ai configuration.
This script verifies the available configuration options for the crawl4ai library.
"""

import asyncio
from inspect import signature, getmro
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from loguru import logger

logger.add(
    "logs/test_crawler_{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention="3 days",
    level="INFO"
)

async def test_crawler_config():
    """Test and document crawl4ai configuration options."""
    logger.info("Testing crawl4ai configuration options")
    
    # Check BrowserConfig
    logger.info("=== BrowserConfig ===")
    browser_sig = signature(BrowserConfig.__init__)
    logger.info(f"Constructor signature: {browser_sig}")
    
    browser_params = list(browser_sig.parameters.keys())
    if "args" in browser_params:
        logger.info("BrowserConfig DOES accept 'args' parameter")
    else:
        logger.info(f"BrowserConfig does NOT accept 'args' parameter. Available parameters: {browser_params}")
    
    # Create a browser config
    try:
        browser_config = BrowserConfig(
            headless=True,
            verbose=True
        )
        logger.info(f"Successfully created browser config: {browser_config}")
    except Exception as e:
        logger.error(f"Error creating browser config: {e}")
    
    # Check CrawlerRunConfig
    logger.info("\n=== CrawlerRunConfig ===")
    run_sig = signature(CrawlerRunConfig.__init__)
    logger.info(f"Constructor signature: {run_sig}")
    
    run_params = list(run_sig.parameters.keys())
    if "browser_args" in run_params:
        logger.info("CrawlerRunConfig DOES accept 'browser_args' parameter")
    else:
        logger.info(f"CrawlerRunConfig does NOT accept 'browser_args' parameter. Available parameters: {run_params}")
    
    # Create a crawler run config
    try:
        browser_args = [
            "--disable-dev-shm-usage",
            "--disable-gpu"
        ]
        
        # Try with browser_args parameter if available
        if "browser_args" in run_params:
            run_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                js_code=["await new Promise(r => setTimeout(r, 1000));"],
                browser_args=browser_args
            )
        else:
            # Try alternative approaches based on available parameters
            run_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                js_code=["await new Promise(r => setTimeout(r, 1000));"]
            )
            
        logger.info(f"Successfully created run config: {run_config}")
        
        # Check if we can set browser_args after creation if it's not a constructor parameter
        if "browser_args" not in run_params and hasattr(run_config, "browser_args"):
            run_config.browser_args = browser_args
            logger.info("Set browser_args attribute after creation")
    except Exception as e:
        logger.error(f"Error creating run config: {e}")
    
    # Try to create and use a web crawler
    logger.info("\n=== AsyncWebCrawler ===")
    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            logger.info("Successfully created web crawler")
            
            # Get available methods
            crawler_methods = [m for m in dir(crawler) if not m.startswith("_")]
            logger.info(f"Available crawler methods: {crawler_methods}")
            
            # Try a simple run with the configured options
            if "browser_args" in run_params:
                result = await crawler.arun(
                    url="https://example.com",
                    config=run_config
                )
            else:
                # Try alternative approaches
                result = await crawler.arun(
                    url="https://example.com",
                    config=run_config
                )
            
            logger.info(f"Crawler run successful: {result.success}")
    except Exception as e:
        logger.error(f"Error testing crawler: {e}")

    logger.info("Crawler configuration testing complete")

if __name__ == "__main__":
    asyncio.run(test_crawler_config()) 