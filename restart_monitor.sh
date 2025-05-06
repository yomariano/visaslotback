#!/bin/bash

# restart_monitor.sh
# Script to monitor and restart the appointment monitoring process if it crashes

MAX_RESTARTS=5
RESTART_COUNT=0
RESTART_WAIT_TIME=120  # Initial wait time in seconds
LOG_FILE="logs/monitor_restart.log"

# Create logs directory if it doesn't exist
mkdir -p logs

log() {
    echo "[$(date +"%Y-%m-%d %H:%M:%S")] $1" | tee -a "$LOG_FILE"
}

log "Starting appointment monitor restart script"

while [ $RESTART_COUNT -le $MAX_RESTARTS ]; do
    log "Starting appointment_monitor.py (attempt $((RESTART_COUNT+1))/$((MAX_RESTARTS+1)))"
    
    # Start the appointment monitor process
    python3 appointment_monitor.py
    
    # Get the exit code
    EXIT_CODE=$?
    
    # Check if process terminated gracefully (Ctrl+C returns 130)
    if [ $EXIT_CODE -eq 0 ] || [ $EXIT_CODE -eq 130 ]; then
        log "Appointment monitor exited cleanly with code $EXIT_CODE. Not restarting."
        break
    fi
    
    # Process crashed, increment restart counter
    RESTART_COUNT=$((RESTART_COUNT+1))
    
    if [ $RESTART_COUNT -le $MAX_RESTARTS ]; then
        log "Appointment monitor crashed with exit code $EXIT_CODE. Restarting in $RESTART_WAIT_TIME seconds..."
        
        # Wait before restarting
        sleep $RESTART_WAIT_TIME
        
        # Increase wait time for next restart (exponential backoff)
        RESTART_WAIT_TIME=$((RESTART_WAIT_TIME*2))
    else
        log "Maximum restart attempts ($MAX_RESTARTS) reached. Giving up."
        break
    fi
done

log "Restart script terminated" 