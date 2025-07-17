# app.py

import asyncio
import logging
from dotenv import load_dotenv
from bot_runner import main as run_bot

# Load environment variables from the .env file.
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logging.info("Bot shutting down by user request.")
    except Exception as e:
        logging.critical("An unhandled exception caused the bot to crash.", exc_info=True)