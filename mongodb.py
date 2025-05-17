from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from loguru import logger
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError, OperationFailure
from pymongo import ASCENDING, DESCENDING, IndexModel
import backoff
import json

# Load environment variables from .env file only if they're not already set
load_dotenv(override=False)

# Schema definitions for data validation
APPOINTMENT_SCHEMA = {
    "bsonType": "object",
    "required": ["city", "timestamp", "countries"],
    "properties": {
        "city": {"bsonType": "string"},
        "timestamp": {"bsonType": "date"},
        "countries": {
            "bsonType": "array",
            "items": {
                "bsonType": "object",
                "required": ["country", "flag", "slots"],
                "properties": {
                    "country": {"bsonType": "string"},
                    "flag": {"bsonType": "string"},
                    "earliest_available": {"bsonType": ["string", "null"]},
                    "slots": {
                        "bsonType": "object",
                        "patternProperties": {
                            "^[A-Z]{3}$": {"bsonType": ["string", "null"]}
                        }
                    }
                }
            }
        },
        "temporarily_unavailable": {
            "bsonType": "array",
            "items": {"bsonType": "string"}
        }
    }
}

class MongoDBClient:
    def __init__(self):
        self.uri = os.getenv("MONGODB_URI")
        self.db_name = os.getenv("MONGODB_DB_NAME", "default")
        self.appointments_collection = os.getenv("MONGODB_COLLECTION_APPOINTMENTS", "appointments")
        self.users_collection = os.getenv("MONGODB_COLLECTION_USERS", "users")
        self.retention_days = int(os.getenv("MONGODB_DATA_RETENTION_DAYS", "90"))
        
        if not self.uri:
            raise ValueError("MongoDB URI not found in environment variables")
        
        logger.info(f"Connecting to MongoDB database: {self.db_name}")
        
        # Configure client with more resilient settings
        self.client = AsyncIOMotorClient(
            self.uri,
            serverSelectionTimeoutMS=30000,  # 30 seconds for server selection
            connectTimeoutMS=30000,          # 30 seconds for initial connection
            socketTimeoutMS=45000,           # 45 seconds for socket operations
            maxPoolSize=50,                  # Increase connection pool size
            minPoolSize=10,                  # Maintain minimum connections
            maxIdleTimeMS=60000,            # Close idle connections after 60 seconds
            waitQueueTimeoutMS=30000,       # How long to wait for available connection
            retryWrites=True,               # Enable automatic retry of write operations
            retryReads=True                 # Enable automatic retry of read operations
        )
        self.db = self.client[self.db_name]

    async def initialize_collections(self):
        """Initialize collections with proper indexes and validation."""
        try:
            # Create indexes for appointments collection
            appointment_indexes = [
                IndexModel([("city", ASCENDING), ("timestamp", DESCENDING)]),
                IndexModel([("timestamp", ASCENDING)], expireAfterSeconds=self.retention_days * 24 * 3600)
            ]
            
            await self.db[self.appointments_collection].create_indexes(appointment_indexes)
            
            # Add schema validation
            await self.db.command({
                "collMod": self.appointments_collection,
                "validator": {"$jsonSchema": APPOINTMENT_SCHEMA},
                "validationLevel": "moderate"
            })
            
            logger.info("Successfully initialized MongoDB collections and indexes")
            return True
        except Exception as e:
            logger.error(f"Error initializing MongoDB collections: {e}")
            return False

    async def is_healthy(self) -> bool:
        """Check if the MongoDB connection is healthy."""
        try:
            await self.client.admin.command('ping')
            return True
        except Exception as e:
            logger.error(f"MongoDB health check failed: {e}")
            return False

    @backoff.on_exception(
        backoff.expo,
        (ConnectionFailure, ServerSelectionTimeoutError),
        max_tries=3,
        max_time=30
    )
    async def save_appointment_data(self, city: str, data: Dict) -> bool:
        """Save appointment data to MongoDB using upsert operation with retry logic."""
        try:
            # Convert string timestamp to datetime if needed
            if isinstance(data.get("timestamp"), str):
                data["timestamp"] = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
            elif "timestamp" not in data:
                data["timestamp"] = datetime.utcnow()
            
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

    async def get_appointment_stats(self, city: str, days: int = 30) -> Dict[str, Any]:
        """Get appointment availability statistics for a city."""
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            
            pipeline = [
                {"$match": {
                    "city": city,
                    "timestamp": {"$gte": cutoff_date}
                }},
                {"$unwind": "$countries"},
                {"$group": {
                    "_id": "$countries.country",
                    "availability_count": {
                        "$sum": {"$cond": [{"$ne": ["$countries.earliest_available", None]}, 1, 0]}
                    },
                    "total_checks": {"$sum": 1},
                    "last_available": {"$last": "$countries.earliest_available"},
                    "avg_slots": {
                        "$avg": {
                            "$sum": {
                                "$map": {
                                    "input": {"$objectToArray": "$countries.slots"},
                                    "as": "slot",
                                    "in": {
                                        "$convert": {
                                            "input": {"$replaceOne": {"input": "$$slot.v", "find": "+", "replacement": ""}},
                                            "to": "int",
                                            "onError": 0
                                        }
                                    }
                                }
                            }
                        }
                    }
                }},
                {"$project": {
                    "country": "$_id",
                    "availability_rate": {
                        "$multiply": [
                            {"$divide": ["$availability_count", "$total_checks"]},
                            100
                        ]
                    },
                    "last_available": 1,
                    "avg_slots": 1
                }},
                {"$sort": {"availability_rate": -1}}
            ]
            
            cursor = self.db[self.appointments_collection].aggregate(pipeline)
            stats = await cursor.to_list(length=None)
            
            return {
                "city": city,
                "period_days": days,
                "countries": stats
            }
        except Exception as e:
            logger.error(f"Error getting appointment stats for {city}: {e}")
            return {"error": str(e)}

    async def cleanup_old_data(self, days: Optional[int] = None) -> int:
        """Manually cleanup old appointment data."""
        try:
            retention_days = days or self.retention_days
            cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
            
            result = await self.db[self.appointments_collection].delete_many({
                "timestamp": {"$lt": cutoff_date}
            })
            
            deleted_count = result.deleted_count
            logger.info(f"Cleaned up {deleted_count} old appointment records")
            return deleted_count
        except Exception as e:
            logger.error(f"Error cleaning up old data: {e}")
            return 0

    async def close(self):
        """Close the MongoDB connection."""
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed")

    async def get_active_subscriptions(self, city: str, country: str) -> List[Dict]:
        """Get all active subscriptions for a city and country combination."""
        try:
            current_time = datetime.utcnow()
            
            # Find users monitoring this city/country combination
            cursor = self.db[self.users_collection].find({
                "cityFrom": city,
                "countryFrom": country
            })
            
            users = await cursor.to_list(length=None)
            active_users = []
            
            # Filter users based on their subscription status
            for user in users:
                payment_date = datetime.fromisoformat(user["paymentDate"].replace("Z", "+00:00"))
                subscription_type = user["subscriptionType"]
                
                # Calculate subscription end date
                if subscription_type == "monthly":
                    end_date = payment_date + timedelta(days=30)
                elif subscription_type == "weekly":
                    end_date = payment_date + timedelta(days=7)
                else:
                    logger.warning(f"Unknown subscription type {subscription_type} for user {user.get('email')}")
                    continue
                
                # Check if subscription is still active
                if current_time <= end_date:
                    active_users.append(user)
            
            logger.info(f"Found {len(active_users)} active subscriptions for {city}/{country}")
            return active_users
        except Exception as e:
            logger.error(f"Error getting active subscriptions for {city}/{country}: {e}")
            return []

    async def detect_slot_changes(self, city: str, current_data: Dict) -> List[Dict[str, Any]]:
        """
        Detect changes in slot availability between current and previous data.
        Returns a list of changes that need notifications.
        """
        try:
            previous_data = await self.get_last_appointment_data(city)
            if not previous_data:
                logger.info(f"No previous data found for {city}, skipping change detection")
                return []
            
            changes = []
            current_countries = {c["country"]: c for c in current_data.get("countries", [])}
            previous_countries = {c["country"]: c for c in previous_data.get("countries", [])}
            
            # Check each country in current data
            for country_name, current_country in current_countries.items():
                previous_country = previous_countries.get(country_name)
                if not previous_country:
                    # New country added
                    if any(v is not None for v in current_country["slots"].values()):
                        changes.append({
                            "city": city,
                            "country": country_name,
                            "change_type": "new_country",
                            "current_slots": current_country["slots"],
                            "previous_slots": None,
                            "url": f"{current_data.get('base_url', '')}/{country_name.lower().replace(' ', '-')}"
                        })
                    continue
                
                # Compare slots for each month
                for month, current_slots in current_country["slots"].items():
                    previous_slots = previous_country["slots"].get(month)
                    
                    # Check if slots became available
                    if (previous_slots is None or previous_slots == "0") and current_slots not in (None, "0"):
                        changes.append({
                            "city": city,
                            "country": country_name,
                            "change_type": "slots_available",
                            "month": month,
                            "current_slots": current_slots,
                            "previous_slots": previous_slots,
                            "url": f"{current_data.get('base_url', '')}/{country_name.lower().replace(' ', '-')}"
                        })
            
            return changes
        except Exception as e:
            logger.error(f"Error detecting slot changes for {city}: {e}")
            return [] 