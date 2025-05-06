import asyncio
from mongodb import MongoDBClient
from loguru import logger

async def test_connection():
    try:
        # Initialize MongoDB client
        db = MongoDBClient()
        logger.info("MongoDB client initialized")
        
        # Test saving some data
        test_data = {
            "test": True,
            "message": "Test connection successful"
        }
        success = await db.save_appointment_data("test_city", test_data)
        logger.info(f"Save test data: {'success' if success else 'failed'}")
        
        # Test retrieving data
        data = await db.get_last_appointment_data("test_city")
        logger.info(f"Retrieved data: {data}")
        
        # Close connection
        await db.close()
        logger.info("Connection test completed successfully")
        
    except Exception as e:
        logger.error(f"Error testing MongoDB connection: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection()) 