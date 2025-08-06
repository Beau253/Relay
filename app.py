# app.py

import asyncio
import logging
from dotenv import load_dotenv

#
# LOAD ENVIRONMENT VARIABLES FIRST
# This line MUST come before any other imports that rely on environment variables.
#
load_dotenv()

from bot_runner import main as run_bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logging.info("Bot shutting down by user request.")
    except Exception as e:
        logging.critical("An unhandled exception caused the bot to crash.", exc_info=True)