FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create logs directory and set permissions
RUN mkdir -p logs && \
    chmod +x start.sh

# Run the application using the startup script
CMD ["./start.sh"] 