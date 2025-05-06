import asyncio
from datetime import datetime, timedelta
from typing import Dict
from mongodb import MongoDBClient
from appointment_monitor import AppointmentMonitor
from notification_service import NotificationService, NotificationData
from loguru import logger

class NotificationWorkflowTester:
    def __init__(self):
        self.db = MongoDBClient()
        self.monitor = AppointmentMonitor()
        self.notification_service = NotificationService()

    async def setup_test_data(self):
        """Set up initial test data in MongoDB"""
        # Test city and country
        city = "Toronto"
        country = "Canada"

        # Initial appointment data
        initial_data = {
            "city": city,
            "countries": [{
                "name": country,
                "slots": ["2024-05-01", "2024-05-15"],
                "earliest_date": "2024-05-01"
            }],
            "temporarily_unavailable": [],
            "timestamp": datetime.now()
        }

        # Test user data
        test_user = {
            "email": "test@example.com",
            "phone": "+1234567890",  # Replace with a valid test phone number
            "cityFrom": city,
            "countryTo": country,
            "subscription": {
                "active": True,
                "expiresAt": datetime.now() + timedelta(days=30)
            }
        }

        # Save test data
        await self.db.save_appointment_data(city, initial_data)
        await self.db.db[self.db.users_collection].insert_one(test_user)
        
        logger.info("Test data setup completed")
        return city, country

    async def simulate_appointment_change(self, city: str, country: str):
        """Simulate a change in appointment data"""
        # New appointment data with different slots
        new_data = {
            "city": city,
            "countries": [{
                "name": country,
                "slots": ["2024-04-15", "2024-04-20", "2024-05-01"],  # Added new slots
                "earliest_date": "2024-04-15"  # Earlier date
            }],
            "temporarily_unavailable": [],
            "timestamp": datetime.now()
        }

        # Save new data
        await self.db.save_appointment_data(city, new_data)
        logger.info("Simulated appointment change completed")

    async def verify_notification_workflow(self):
        """Test the entire notification workflow"""
        try:
            # 1. Set up test data
            city, country = await self.setup_test_data()
            logger.info(f"Testing workflow for {city}, {country}")

            # 2. Simulate a change in appointments
            await self.simulate_appointment_change(city, country)
            logger.info("Appointment change simulated")

            # 3. Run the monitor to detect changes
            changes = await self.monitor.detect_changes(city, await self.db.get_last_appointment_data(city))
            logger.info(f"Detected changes: {changes}")

            # 4. Get users to notify
            users = await self.db.get_users_by_city(city)
            logger.info(f"Found {len(users)} users to notify")

            # 5. Send test notifications
            if changes and users:
                for user in users:
                    notification_data = NotificationData(
                        city=city,
                        changes=changes,
                        timestamp=datetime.now()
                    )
                    await self.notification_service.notify_user(
                        email=user["email"],
                        phone=user["phone"],
                        data=notification_data
                    )
                logger.info("Notifications sent successfully")

            return True

        except Exception as e:
            logger.error(f"Error in notification workflow test: {e}")
            return False

    async def cleanup_test_data(self):
        """Clean up test data from MongoDB"""
        try:
            # Remove test appointment data
            await self.db.db[self.db.appointments_collection].delete_many({"city": "Toronto"})
            # Remove test user data
            await self.db.db[self.db.users_collection].delete_many({"email": "test@example.com"})
            logger.info("Test data cleaned up")
        except Exception as e:
            logger.error(f"Error cleaning up test data: {e}")

async def main():
    tester = NotificationWorkflowTester()
    try:
        success = await tester.verify_notification_workflow()
        if success:
            logger.info("Notification workflow test completed successfully")
        else:
            logger.error("Notification workflow test failed")
    finally:
        await tester.cleanup_test_data()
        await tester.db.close()

if __name__ == "__main__":
    asyncio.run(main()) 