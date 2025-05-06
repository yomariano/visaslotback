import os
from notification_service import NotificationService, NotificationData
from datetime import datetime
import asyncio

async def test_sms():
    service = NotificationService()
    test_data = NotificationData(
        city="Dublin",
        changes={
            "USA": {
                "new_slots": 3,
                "earliest_date": "2024-04-01"
            }
        },
        timestamp=datetime.now()
    )
    
    # Test SMS only
    result = await service.send_sms("+353838454183", test_data)
    print(f"SMS sent successfully: {result}")

if __name__ == "__main__":
    asyncio.run(test_sms()) 