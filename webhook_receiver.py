import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from datetime import datetime
import os
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from sse_starlette.sse import EventSourceResponse
import asyncio

# Load environment variables
load_dotenv()

app = FastAPI(title="Appointment Monitor Webhook Receiver")

# Setup CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Define model for the webhook payload
class AppointmentAnalysis(BaseModel):
    new_slots: List[str] = []
    removed_slots: List[str] = []
    location_changes: List[str] = []
    other_changes: List[str] = []
    summary: str
    timestamp: Optional[str] = None

class WebhookPayload(BaseModel):
    timestamp: str
    current_data: Dict[str, Any]
    previous_data: Optional[Dict[str, Any]] = None
    analysis: AppointmentAnalysis

# In-memory storage for received webhooks (for demonstration purposes)
webhook_history = []

# Event store for SSE
STREAM_DELAY = 1  # second
subscribers = set()

async def publish_update(payload):
    """Publish update to all subscribers."""
    if subscribers:
        for subscriber in subscribers:
            await subscriber.put(json.dumps(payload))

@app.post("/webhook")
async def receive_webhook(request: Request):
    """Endpoint to receive webhook data from the appointment monitor."""
    try:
        payload = await request.json()
        # Add to history (limit to last 100 events to avoid memory issues)
        webhook_history.append({
            "received_at": datetime.now().isoformat(),
            "payload": payload
        })
        
        if len(webhook_history) > 100:
            webhook_history.pop(0)
        
        # Log the receipt
        print(f"Received webhook at {datetime.now().isoformat()}")
        print(f"Analysis summary: {payload.get('analysis', {}).get('summary', 'No summary available')}")
        
        # Publish to subscribers
        await publish_update(payload)
        
        return JSONResponse(content={"status": "success", "message": "Webhook received"})
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {str(e)}")

@app.get("/history")
async def get_webhook_history():
    """Endpoint to retrieve the history of received webhooks."""
    return webhook_history

@app.get("/stream")
async def stream_updates(request: Request):
    """Stream updates using Server-Sent Events (SSE) for real-time frontend updates."""
    subscriber_queue = asyncio.Queue()
    subscribers.add(subscriber_queue)
    
    async def event_generator():
        try:
            while True:
                # Send initial connection message
                if len(webhook_history) > 0:
                    yield {
                        "event": "initial", 
                        "data": json.dumps({"history": webhook_history[-1]})
                    }
                
                # Wait for new updates
                try:
                    update = await asyncio.wait_for(subscriber_queue.get(), timeout=30)
                    yield {"event": "update", "data": update}
                except asyncio.TimeoutError:
                    # Send keepalive event every 30 seconds
                    yield {"event": "ping", "data": ""}
                
                await asyncio.sleep(STREAM_DELAY)
        except asyncio.CancelledError:
            # Client disconnected
            pass
        finally:
            subscribers.remove(subscriber_queue)
    
    return EventSourceResponse(event_generator())

@app.get("/")
async def root():
    """Root endpoint with basic info."""
    return {
        "service": "Appointment Monitor Webhook Receiver",
        "endpoints": {
            "/webhook": "POST - Receive webhook data",
            "/history": "GET - Retrieve webhook history",
            "/stream": "GET - Stream updates in real-time using SSE"
        },
        "webhooks_received": len(webhook_history)
    }

if __name__ == "__main__":
    # Get port from environment or use default
    port = int(os.getenv("WEBHOOK_PORT", 8000))
    
    print(f"Starting webhook receiver on port {port}")
    print(f"Webhook URL: http://localhost:{port}/webhook")
    print("To view webhook history, visit: http://localhost:{port}/history")
    print("To stream updates in real-time, connect to: http://localhost:{port}/stream")
    
    uvicorn.run(app, host="0.0.0.0", port=port) 