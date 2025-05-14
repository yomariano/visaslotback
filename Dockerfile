FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install --with-deps

# Copy application files
COPY . .

# Create logs directory and set permissions
RUN mkdir -p logs && \
    chmod +x start.sh

# Run the application using the startup script
CMD ["./start.sh"] 