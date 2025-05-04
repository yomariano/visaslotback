from typing import Dict, List, Optional
from datetime import datetime
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from loguru import logger

# Load environment variables
load_dotenv()

class MongoDBClient:
    def __init__(self):
        self.uri = os.getenv("MONGODB_URI")
        self.db_name = os.getenv("MONGODB_DB_NAME", "default")
        self.appointments_collection = os.getenv("MONGODB_COLLECTION_APPOINTMENTS", "appointments")
        self.users_collection = os.getenv("MONGODB_COLLECTION_USERS", "users")
        
        if not self.uri:
            raise ValueError("MongoDB URI not found in environment variables")
        
        self.client = AsyncIOMotorClient(self.uri)
        self.db = self.client[self.db_name]
        
    async def save_appointment_data(self, city: str, data: Dict) -> bool:
        """Save appointment data to MongoDB using upsert operation."""
        try:
            # Add timestamp if not present
            if "timestamp" not in data:
                data["timestamp"] = datetime.now()
            
            # Add city as a key field if not present
            data["city"] = city
            
            # Use update_one with upsert=True to update existing record or create new one
            await self.db[self.appointments_collection].update_one(
                {"city": city},  # filter by city
                {"$set": data},  # update data
                upsert=True  # create if doesn't exist
            )
            logger.info(f"Updated appointment data for {city}")
            return True
        except Exception as e:
            logger.error(f"Error saving appointment data for {city}: {e}")
            return False

    async def get_last_appointment_data(self, city: str) -> Optional[Dict]:
        """Get the most recent appointment data for a city."""
        try:
            result = await self.db[self.appointments_collection].find_one(
                {"city": city},
                sort=[("timestamp", -1)]
            )
            return result
        except Exception as e:
            logger.error(f"Error getting last appointment data for {city}: {e}")
            return None

    async def get_users_by_city(self, city: str) -> List[Dict]:
        """Get all users monitoring a specific city."""
        try:
            cursor = self.db[self.users_collection].find({"cityFrom": city})
            users = await cursor.to_list(length=None)
            logger.info(f"Found {len(users)} users monitoring city: {city}")
            return users
        except Exception as e:
            logger.error(f"Error getting users for city {city}: {e}")
            return []

    async def close(self):
        """Close the MongoDB connection."""
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed") 