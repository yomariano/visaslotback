# Schengen Appointment Monitor

This system monitors the Schengen appointment website for changes in appointment availability and sends structured JSON data to a webhook endpoint.

## Features

- Monitors schengenappointments.com for appointment availability changes
- Uses Google Gemini AI and OpenAI via OpenRouter for data extraction
- Sends webhook notifications with structured JSON data
- Includes a sample webhook receiver for demonstration

## Setup

### 1. Environment Setup

First, copy the example environment file:

```bash
cp env.example .env
```

Edit the `.env` file and add your API keys and configure the webhook URL:

```
GOOGLE_API_KEY=your_google_gemini_api_key_here
OPENROUTER_API_KEY=your_openrouter_api_key_here
WEBHOOK_URL=http://localhost:8000/webhook
WEBHOOK_PORT=8000
```

#### Obtaining API Keys

- **OpenRouter API Key**: Visit [OpenRouter](https://openrouter.ai/) to create an account and generate an API key.
  - OpenRouter provides a unified API to access OpenAI models and others
  - The system uses OpenRouter to access GPT-4-nano for web data extraction

- **Google Gemini API Key**: Visit [Google AI Studio](https://makersuite.google.com/app/apikey) to create an API key.
  - Used as an alternative for certain extraction tasks

### 2. Install Dependencies

Install the required Python packages:

```bash
pip install -r requirements.txt
```

### 3. Run the Webhook Receiver

Start the webhook receiver (in a separate terminal):

```bash
python webhook_receiver.py
```

This will start a FastAPI server on port 8000 (or the port specified in your `.env` file).

### 4. Run the Appointment Monitor

Start the appointment monitor:

```bash
python appointment_monitor.py
```

The monitor will check for appointment changes every minute and send notifications to your webhook endpoint.

## Webhook Data Structure

The webhook endpoint will receive POST requests with JSON data in the following format:

```json
{
  "timestamp": "2023-04-20T15:30:45.123456",
  "current_data": {
    "available_slots": [],
    "locations": []
  },
  "previous_data": {
    "available_slots": [],
    "locations": []
  },
  "analysis": {
    "new_slots": [],
    "removed_slots": [],
    "location_changes": [],
    "other_changes": [],
    "summary": "A brief summary of the changes",
    "timestamp": "2023-04-20T15:30:45.123456"
  }
}
```

## Webhook Receiver API

The sample webhook receiver provides these endpoints:

- `POST /webhook` - Receives webhook data from the appointment monitor
- `GET /history` - Retrieves the history of received webhooks
- `GET /` - Root endpoint with basic info

## Customization

### Custom Webhook Integration

To integrate with your own webhook handler, simply set the `WEBHOOK_URL` in the `.env` file to your endpoint. Your endpoint should accept POST requests with JSON data.

### Adjusting Monitoring Frequency

To change how often the system checks for appointment changes, modify the scheduler line in `appointment_monitor.py`:

```python
schedule.every(1).minutes.do(lambda: asyncio.create_task(monitor.monitor_appointments()))
```

Change `1` to your desired interval in minutes.

## Troubleshooting

### Webhook Not Sending

- Ensure `WEBHOOK_URL` is correctly set in your `.env` file
- Check that your webhook receiver is running and accessible at the URL

### AI API Issues

- Verify your `GOOGLE_API_KEY` is valid and has access to the Gemini API
- Verify your `OPENROUTER_API_KEY` is valid and has sufficient credit 
- Check if you see errors like `The api_key client option must be set` in logs, which indicates missing API keys
- Check the logs for specific error messages

### Browser Agent Failures

- If the agent fails to extract data, check Chrome installation
- Try running with a non-headless browser for debugging

## License

This project is provided as-is without any warranty. Use at your own risk. 