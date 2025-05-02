import os
from typing import List, Dict
from dataclasses import dataclass
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

@dataclass
class NotificationData:
    city: str
    changes: Dict
    timestamp: datetime

class NotificationService:
    def __init__(self):
        # Email settings
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_username = os.getenv("SMTP_USERNAME")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        
        # Twilio settings
        self.twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.twilio_from_number = os.getenv("TWILIO_FROM_NUMBER")
        
        # Initialize Twilio client if credentials are available
        self.twilio_client = None
        if all([self.twilio_account_sid, self.twilio_auth_token]):
            self.twilio_client = Client(self.twilio_account_sid, self.twilio_auth_token)

    def format_notification_message(self, data: NotificationData) -> str:
        """Format the notification message for both email and SMS."""
        message = f"Visa Slot Update for {data.city}\n\n"
        
        for country, details in data.changes.items():
            message += f"{country}:\n"
            if "new_slots" in details:
                message += f"- New slots available: {details['new_slots']}\n"
            if "earliest_date" in details:
                message += f"- Earliest available date: {details['earliest_date']}\n"
            message += "\n"
        
        return message

    async def send_email(self, to_email: str, data: NotificationData) -> bool:
        """Send email notification."""
        try:
            message = MIMEMultipart()
            message["From"] = self.smtp_username
            message["To"] = to_email
            message["Subject"] = f"Visa Slot Update - {data.city}"
            
            body = self.format_notification_message(data)
            message.attach(MIMEText(body, "plain"))
            
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_username, self.smtp_password)
                server.send_message(message)
            
            return True
        except Exception as e:
            print(f"Error sending email: {e}")
            return False

    async def send_sms(self, to_phone: str, data: NotificationData) -> bool:
        """Send SMS notification."""
        if not self.twilio_client:
            print("Twilio client not configured")
            return False
        
        try:
            message = self.format_notification_message(data)
            self.twilio_client.messages.create(
                body=message,
                from_=self.twilio_from_number,
                to=to_phone
            )
            return True
        except Exception as e:
            print(f"Error sending SMS: {e}")
            return False

    async def notify_user(self, email: str, phone: str, data: NotificationData) -> None:
        """Send notifications to a user through both email and SMS."""
        email_sent = await self.send_email(email, data)
        sms_sent = await self.send_sms(phone, data)
        
        print(f"Notifications sent - Email: {email_sent}, SMS: {sms_sent}") 