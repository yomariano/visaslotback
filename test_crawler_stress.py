import asyncio
import time
import sys
import gc
import logging
from loguru import logger
from appointment_monitor import AppointmentMonitor, recover_from_recursion_error

# Configure logging to show detailed information
logger.remove()
logger.add(sys.stderr, level="DEBUG")
logger.add("logs/stress_test_{time}.log", level="DEBUG")

async def run_stress_test(batches=3, cities_per_batch=5, forced_errors=True):
    """
    Run a stress test on the appointment monitor with configurable parameters.
    
    Args:
        batches: Number of batches to process
        cities_per_batch: Cities to process per batch
        forced_errors: Whether to deliberately inject errors to test robustness
    """
    logger.info(f"Starting stress test with {batches} batches, {cities_per_batch} cities per batch")
    logger.info(f"Forced errors: {forced_errors}")
    
    # Create monitor instance
    monitor = AppointmentMonitor()
    
    # Keep track of stats
    stats = {
        "batches_started": 0,
        "batches_completed": 0,
        "cities_processed": 0,
        "errors_encountered": 0,
        "recursion_errors": 0,
        "recoveries_attempted": 0,
        "successful_recoveries": 0,
    }
    
    # Override some methods to inject errors if enabled
    if forced_errors:
        # Save original methods
        original_setup = monitor.setup_crawler
        original_extract = monitor.extract_appointment_data
        
        # Counter for calls to track when to inject errors
        call_counters = {"setup": 0, "extract": 0}
        
        async def error_injecting_setup(*args, **kwargs):
            """Wrapper around setup_crawler that occasionally injects errors"""
            call_counters["setup"] += 1
            
            # Every 3rd call, cause a problem
            if call_counters["setup"] % 3 == 0:
                logger.warning("INJECTED: Simulating setup failure")
                stats["errors_encountered"] += 1
                return False
                
            # Every 5th call, trigger a recursion-like error
            if call_counters["setup"] % 5 == 0:
                logger.warning("INJECTED: Simulating recursion error")
                stats["errors_encountered"] += 1
                stats["recursion_errors"] += 1
                
                # Use the recovery mechanism
                stats["recoveries_attempted"] += 1
                recovery_success = await recover_from_recursion_error(monitor)
                if recovery_success:
                    stats["successful_recoveries"] += 1
                
                # Return success/failure based on recovery
                return recovery_success
            
            # Otherwise use original
            return await original_setup(*args, **kwargs)
            
        async def error_injecting_extract(city, *args, **kwargs):
            """Wrapper around extract_appointment_data that occasionally injects errors"""
            call_counters["extract"] += 1
            
            # Every 4th call, simulate a network error
            if call_counters["extract"] % 4 == 0:
                logger.warning(f"INJECTED: Simulating network error for {city}")
                stats["errors_encountered"] += 1
                return {"city": city, "error": "Simulated network error", "timestamp": time.time()}
            
            # Otherwise use original
            return await original_extract(city, *args, **kwargs)
            
        # Override methods with our error-injecting versions
        monitor.setup_crawler = error_injecting_setup
        monitor.extract_appointment_data = error_injecting_extract
    
    # Create a modified version of monitor_appointments that uses our test parameters
    async def test_monitor_appointments():
        """Modified version of monitor_appointments for testing"""
        # Skip the check for monitoring_in_progress to allow running when needed
        monitor.monitoring_in_progress = True
        monitor.last_monitoring_start = time.time()
        logger.info("Starting test monitoring cycle...")
        
        try:
            # Instead of using the real city list, create a test list
            all_cities = []
            for i in range(cities_per_batch * batches):
                country = "TestCountry"
                city = f"TestCity{i+1}"
                all_cities.append((country, city))
            
            # Process cities in batches
            for i in range(0, len(all_cities), cities_per_batch):
                batch = all_cities[i:i+cities_per_batch]
                batch_num = i//cities_per_batch + 1
                total_batches = (len(all_cities) + cities_per_batch - 1) // cities_per_batch
                logger.info(f"Processing batch {batch_num} of {total_batches}")
                
                stats["batches_started"] += 1
                
                # CENTRALIZED CRAWLER MANAGEMENT: Ensure clean state at beginning of batch
                monitor.crawler = None
                monitor._initializing_crawler = False
                
                # Try to initialize crawler for this batch - ONCE per batch
                try:
                    # For testing, inject occasional crawler initialization failures
                    crawler_initialized = await monitor.setup_crawler()
                    
                    if not crawler_initialized:
                        logger.error(f"Failed to initialize crawler for batch {batch_num}, skipping batch")
                        await asyncio.sleep(2)
                        continue
                    
                except Exception as e:
                    logger.critical(f"Error during batch initialization: {e}")
                    
                    # Try to recover
                    stats["recoveries_attempted"] += 1
                    recovery_successful = await recover_from_recursion_error(monitor)
                    if recovery_successful:
                        stats["successful_recoveries"] += 1
                        logger.info(f"Recovery successful")
                    else:
                        logger.critical(f"Failed to recover from error")
                    
                    # Skip current batch after errors, even if recovery was successful
                    await asyncio.sleep(2)
                    continue
                
                # Process each city in the batch using same crawler instance
                batch_success = True
                for country, city in batch:
                    logger.info(f"Processing {city}, {country}...")
                    
                    # Safety check: if crawler became None, skip rest of batch
                    if monitor.crawler is None:
                        logger.error(f"Crawler became None unexpectedly during batch {batch_num}, skipping rest of batch.")
                        batch_success = False
                        break
                    
                    try:
                        # For testing, just log instead of actually crawling
                        stats["cities_processed"] += 1
                        
                        # In stress test mode, we simulate processing each city 
                        # without actually crawling the real site
                        if not forced_errors:
                            # Just simulate some work without network calls
                            await asyncio.sleep(0.5)
                            logger.info(f"Successfully processed {city}")
                        else:
                            # Use our error-injecting extract method
                            result = await monitor.extract_appointment_data(city)
                            if "error" in result:
                                logger.warning(f"Error processing {city}: {result['error']}")
                            else:
                                logger.info(f"Successfully processed {city}")
                        
                    except Exception as e:
                        logger.error(f"Error processing {city}: {e}")
                        stats["errors_encountered"] += 1
                    
                    # Add a small delay between cities
                    await asyncio.sleep(0.5)
                
                # CENTRALIZED CRAWLER CLEANUP: Always clean up at end of batch
                logger.info(f"Completed batch {batch_num}, performing cleanup for this batch.")
                await monitor.cleanup_crawler()
                
                # Force garbage collection after each batch
                gc.collect()
                
                stats["batches_completed"] += 1
                
                # Sleep between batches
                sleep_time = 2 if not batch_success else 1
                await asyncio.sleep(sleep_time)
            
            logger.info("Test monitoring cycle completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error in test monitoring cycle: {e}")
            return False
        finally:
            # Ensure crawler is cleaned up
            await monitor.cleanup_crawler()
            
            # Reset monitoring flag
            monitor.monitoring_in_progress = False
            
    # Run the test monitor appointments function
    logger.info("Starting stress test...")
    start_time = time.time()
    
    try:
        success = await test_monitor_appointments()
        
        # Log stats
        logger.info(f"Stress test {'succeeded' if success else 'failed'}")
        logger.info(f"Statistics:")
        for key, value in stats.items():
            logger.info(f"  {key}: {value}")
            
        # Calculate success rate
        if stats["batches_started"] > 0:
            batch_success_rate = (stats["batches_completed"] / stats["batches_started"]) * 100
            logger.info(f"Batch success rate: {batch_success_rate:.2f}%")
            
        if stats["recoveries_attempted"] > 0:
            recovery_success_rate = (stats["successful_recoveries"] / stats["recoveries_attempted"]) * 100
            logger.info(f"Recovery success rate: {recovery_success_rate:.2f}%")
        
    except Exception as e:
        logger.critical(f"Stress test failed with exception: {e}")
    finally:
        # Clean up
        await monitor.cleanup()
        
    elapsed = time.time() - start_time
    logger.info(f"Stress test completed in {elapsed:.2f} seconds")

async def main():
    """Main function to run stress tests with different configurations"""
    # Test 1: Basic stability test without forced errors
    await run_stress_test(batches=3, cities_per_batch=5, forced_errors=False)
    
    # Brief pause between tests
    await asyncio.sleep(2)
    
    # Test 2: Stress test with forced errors to test recovery mechanisms
    await run_stress_test(batches=5, cities_per_batch=7, forced_errors=True)
    
    # Test 3: Intense stress test with more batches
    await run_stress_test(batches=10, cities_per_batch=3, forced_errors=True)

if __name__ == "__main__":
    asyncio.run(main()) 