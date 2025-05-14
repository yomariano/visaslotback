import os
from typing import List, Dict
from dataclasses import dataclass
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from twilio.rest import Client
from dotenv import load_dotenv

# Load environment variables from .env file only if they're not already set
load_dotenv(override=False)

@dataclass
class NotificationData:
    city: str
    country: str
    message: str
    change_type: str
    url: str
    timestamp: datetime

class NotificationService:
    def __init__(self):
        # Email settings
        self.smtp_server = os.getenv("SMTP_SERVER", "mail.privateemail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))  # Namecheap uses port 587 for TLS
        self.smtp_username = os.getenv("SMTP_USERNAME")  # Your full email: notifications@visaslot.xyz
        self.smtp_password = os.getenv("SMTP_PASSWORD")  # Your email account password
        
        # Twilio settings
        self.twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.twilio_from_number = os.getenv("TWILIO_FROM_NUMBER")
        
        # Initialize Twilio client if credentials are available
        self.twilio_client = None
        if all([self.twilio_account_sid, self.twilio_auth_token]):
            self.twilio_client = Client(self.twilio_account_sid, self.twilio_auth_token)

    async def send_email(self, to_email: str, data: NotificationData) -> bool:
        """Send email notification."""
        try:
            message = MIMEMultipart()
            message["From"] = self.smtp_username
            message["To"] = to_email
            message["Subject"] = f"Visa Slot Update - {data.city} ({data.country})"
            
            # Use the pre-formatted message
            message.attach(MIMEText(data.message, "plain"))
            
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
            # Use a shorter version of the message for SMS
            sms_message = data.message
            # Limit SMS length if needed
            if len(sms_message) > 1000:
                sms_message = sms_message[:997] + "..."
                
            self.twilio_client.messages.create(
                body=sms_message,
                from_=self.twilio_from_number,
                to=to_phone
            )
            return True
        except Exception as e:
            print(f"Error sending SMS: {e}")
            return False

    async def notify_user(self, email: str, phone: str, data: NotificationData) -> None:
        """Send notifications to a user through both email and SMS."""
        email_sent = False
        sms_sent = False
        
        if email:
            email_sent = await self.send_email(email, data)
        
        if phone:
            sms_sent = await self.send_sms(phone, data)
        
        print(f"Notifications sent - Email: {email_sent}, SMS: {sms_sent}") 