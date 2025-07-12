# app.py

import os
import asyncio
import threading
from flask import Flask
from dotenv import load_dotenv

from bot_runner import run_bot
from core import ShutdownForBotRotation # Import our custom exception

# Load environment variables from the .env file.
load_dotenv()

# --- Flask Web Server for Keep-Alive ---
app = Flask(__name__)

@app.route('/health')
def health_check():
    """A simple endpoint that uptime services can ping."""
    return "OK", 200

def run_flask():
    """Runs the Flask app."""
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Main Application Execution ---
if __name__ == "__main__":
    # Run the Flask app in a daemon thread.
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Run the Discord bot in the main thread with proper exception handling.
    try:
        asyncio.run(run_bot()) 
    except ShutdownForBotRotation:
        print("Shutdown signal received for bot rotation. The process will now exit. Render will restart it.")
    except KeyboardInterrupt:
        print("Bot shutting down by user request.")