# bot_runner.py

import os
import asyncio
import discord
from discord.ext import commands
import logging

# Import all our core services and the custom exception
from core import (
    DatabaseManager,
    TextTranslator,
    UsageManager,
    GoogleProjectPoolManager,
    send_error_report,
    get_current_version,
    BotLocalizer
)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# --- Environment Variable Loading ---
BOT_MODE = os.getenv('BOT_MODE', 'development')
BOT_TOKEN = os.getenv('BOT_TOKEN')
BOT_VERSION = get_current_version()


class RelayBot(commands.Bot):
    def __init__(self, intents):
        log.info("[RELAYBOT] Initializing bot subclass...")
        super().__init__(command_prefix="!", intents=intents)

        # Create manager instances but DO NOT initialize them here.
        log.info("[RELAYBOT] Creating manager instances...")
        self.db_manager = DatabaseManager()
        self.translator = TextTranslator()
        self.gcp_pool_manager = GoogleProjectPoolManager(self.db_manager)
        self.usage_manager = UsageManager(self.db_manager, self.gcp_pool_manager)
        log.info("[RELAYBOT] Manager instances created.")

    async def on_ready(self):
        log.info("="*50)
        log.info(f"Relay is online. Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Version: {BOT_VERSION} | Mode: {BOT_MODE}")
        log.info("="*50)

    async def setup_hook(self):
        """
        This is the guaranteed entry point for all async setup. It runs after login
        but before on_ready. This is where we will load cogs and sync the tree.
        """
        log.info("--- [SETUP HOOK] Starting guaranteed async setup ---")

        # Step 1: Initialize core services (in dependency order)
        log.info("[SETUP HOOK] Step 1: Initializing Core Services...")
        try:
            # We don't need to re-initialize bot_pool_manager as it was needed for the token
            await self.db_manager.initialize()
            await self.gcp_pool_manager.initialize(self.translator)
            await self.usage_manager.initialize()
            log.info("[SETUP HOOK] ✅ Core Services Initialized.")
        except Exception as e:
            log.critical(f"[SETUP HOOK] ❌ FAILED to initialize core services: {e}", exc_info=True)
            await self.close()
            return

        # Step 2: Set the localizer for the command tree
        log.info("[SETUP HOOK] Step 2: Setting command tree translator...")
        localizer = BotLocalizer()
        await self.tree.set_translator(localizer)
        log.info("[SETUP HOOK] ✅ Translator set.")

        # Step 3: Load all cogs from the /cogs directory
        log.info("[SETUP HOOK] Step 3: Loading Cogs...")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cogs_dir = os.path.join(script_dir, 'cogs')
        for filename in os.listdir(cogs_dir):
            if filename.endswith('.py') and not filename.startswith('__'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    log.info(f"  -> ✅ Successfully loaded cog: {filename}")
                except Exception as e:
                    log.error(f"  -> ❌ Failed to load cog: {filename}", exc_info=e)
        log.info("[SETUP HOOK] ✅ Cog loading complete.")
        
        # Step 4: Sync the command tree AFTER all cogs have been loaded
        log.info("[SETUP HOOK] Step 4: Syncing command tree...")
        try:
            synced_commands = await self.tree.sync()
            log.info(f"[SETUP HOOK] ✅ Command tree synced successfully. {len(synced_commands)} commands registered.")
        except Exception as e:
            log.critical(f"[SETUP HOOK] ❌ FAILED TO SYNC COMMANDS: {e}", exc_info=True)

        log.info("--- [SETUP HOOK] Finished ---")

async def main():
    """Main entry point."""
    log.info("[MAIN] Script starting...")
    if not BOT_TOKEN:
        log.critical("FATAL: BOT_TOKEN environment variable not set. Bot cannot start.")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.reactions = True

    bot = RelayBot(intents=intents)

    try:
        log.info("[MAIN] Attempting to start bot...")
        await bot.start(BOT_TOKEN)
    except Exception as e:
        log.critical("An unhandled exception in main caused a fatal crash.", exc_info=True)
    finally:
        log.info("[MAIN] Closing database connection pool.")
        if bot.db_manager and bot.db_manager.is_initialized:
            await bot.db_manager.close()