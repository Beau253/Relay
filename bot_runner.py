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
    BotPoolManager,
    GoogleProjectPoolManager,
    ShutdownForBotRotation,
    send_error_report,
    get_current_version,
    BotLocalizer
)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# --- Environment Variable Loading ---
BOT_MODE = os.getenv('BOT_MODE', 'development')
BOT_VERSION = get_current_version()


class RelayBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize managers with no dependencies first
        self.db_manager = DatabaseManager()
        self.translator = TextTranslator()
        
        # Initialize managers that depend on the above
        self.gcp_pool_manager = GoogleProjectPoolManager(self.db_manager)
        self.usage_manager = UsageManager(self.db_manager, self.gcp_pool_manager)
        self.bot_pool_manager = BotPoolManager(self.db_manager, self.usage_manager)

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Version: {BOT_VERSION} | Mode: {BOT_MODE}")
        log.info("--------------------------------------------------")
        log.info(">>> Bot startup complete. Relay is now online! <<<")
        log.info("==================================================")
        
    async def setup_hook(self):
        """
        This is the guaranteed entry point for all async setup. It runs after login
        but before on_ready. This is where we load cogs and sync the tree.
        """
        log.info("--- [SETUP HOOK] Starting async setup ---")
        
        # 1. Initialize core services (in dependency order)
        log.info("[SETUP HOOK] Initializing Core Services...")
        await self.db_manager.initialize()
        await self.gcp_pool_manager.initialize(self.translator)
        await self.usage_manager.initialize()
        await self.bot_pool_manager.initialize()
        log.info("[SETUP HOOK] Core Services Initialized.")

        # 2. Set the localizer for the command tree
        log.info("[SETUP HOOK] Setting command tree translator...")
        localizer = BotLocalizer()
        await self.tree.set_translator(localizer)
        log.info("[SETUP HOOK] Translator set.")

        # 3. Load all cogs from the /cogs directory
        log.info("[SETUP HOOK] Loading Cogs...")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cogs_dir = os.path.join(script_dir, 'cogs')
        for filename in os.listdir(cogs_dir):
            if filename.endswith('.py') and not filename.startswith('__'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    log.info(f" -> Successfully loaded cog: {filename}")
                except Exception as e:
                    log.error(f" -> Failed to load cog: {filename}", exc_info=e)
        log.info("[SETUP HOOK] Cog loading complete.")
        
        # 4. Sync the command tree AFTER all cogs have been loaded
        log.info("[SETUP HOOK] Syncing command tree...")
        guild_ids_str = os.getenv("GUILD_IDS")
        if guild_ids_str:
            guild_ids = [int(gid.strip()) for gid in guild_ids_str.split(',') if gid.strip().isdigit()]
            for guild_id in guild_ids:
                try:
                    guild = discord.Object(id=guild_id)
                    await self.tree.sync(guild=guild)
                    log.info(f"Commands successfully synced to Guild ID: {guild_id}")
                except Exception as e:
                    log.error(f"Failed to sync commands to Guild ID {guild_id}: {e}")
        else:
            log.warning("GUILD_IDS environment variable not set. Performing global command sync.")
            await self.tree.sync()
        log.info("[SETUP HOOK] Command tree synced.")
        log.info("--- [SETUP HOOK] Finished ---")

# --- Main Bot Runner Function ---
async def run_bot():
    """
    Initializes and runs the Discord bot.
    """
    # Define the intents required for the bot's features.
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.reactions = True

    # Create an instance of our new bot class
    bot = RelayBot(command_prefix="!", intents=intents)
    
    # --- Get Active Token from the Pool Manager ---
    # This must be done after bot instantiation but before login
    try:
        # We need to manually initialize the pool manager here to get the token
        await bot.db_manager.initialize()
        await bot.usage_manager.db.initialize() # Ensure db is ready for usage manager
        await bot.bot_pool_manager.initialize()
        active_token = await bot.bot_pool_manager.get_active_token()
    except ShutdownForBotRotation as e:
        log.warning(f"Shutdown signal received during startup: {e}")
        raise e
    
    if not active_token:
        log.critical("FATAL: No active token could be determined by the BotPoolManager.")
        return

    # --- Run the Bot with Graceful Shutdown ---
    try:
        await bot.start(active_token)
    finally:
        log.info("Closing database connection pool.")
        await bot.db_manager.close()

# --- OLD Main Bot Runner Function ---
#async def run_bot():
#    """
#    Initializes and runs the Discord bot.
#    """
#    # --- Initialize Core Services (in dependency order) ---
#    # 1. Services with no dependencies
#    db_manager = DatabaseManager()
#    translator = TextTranslator()
#
#    # 2. Services that depend on the above
#    gcp_pool_manager = GoogleProjectPoolManager(db_manager)
#    usage_manager = UsageManager(db_manager, gcp_pool_manager)
#    bot_pool_manager = BotPoolManager(db_manager, usage_manager)
#
#    # --- Await Initialization (in dependency order) ---
#    await db_manager.initialize()
#    await gcp_pool_manager.initialize(translator) # Must be initialized before usage_manager
#    await usage_manager.initialize()
#    await bot_pool_manager.initialize()
#
#    # --- Get Active Token from the Pool Manager ---
#    try:
#        active_token = await bot_pool_manager.get_active_token()
#    except ShutdownForBotRotation as e:
#        log.warning(f"Shutdown signal received during startup: {e}")
#        # Re-raise to be caught by app.py for a clean exit
#        raise e
#    
#    if not active_token:
#        log.critical("FATAL: No active token could be determined by the BotPoolManager.")
#        return
#
#    # Define the intents required for the bot's features.
#    intents = discord.Intents.default()
#    intents.message_content = True
#    intents.members = True
#    intents.reactions = True
#
#    # Create the bot instance.
#    bot = commands.Bot(command_prefix="!", intents=intents)
#    
#    localizer = BotLocalizer()
#    await bot.tree.set_translator(localizer)
#    
#    # --- Attach Core Services to the Bot Instance ---
#    bot.db_manager = db_manager
#    bot.translator = translator
#    bot.usage_manager = usage_manager
#    bot.bot_pool_manager = bot_pool_manager
#    bot.gcp_pool_manager = gcp_pool_manager
#
#    # --- Register the Global Error Handler ---
#    @bot.event
#    async def on_ready():
#        log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
#        log.info(f"Version: {BOT_VERSION} | Mode: {BOT_MODE}")
#        log.info("--------------------------------------------------")
#    
#        # This is the older, looping sync method that is compatible with your library
#        guild_ids_str = os.getenv("GUILD_IDS")
#        if guild_ids_str:
#            guild_ids = [int(gid.strip()) for gid in guild_ids_str.split(',') if gid.strip().isdigit()]
#            if guild_ids:
#                log.info(f"Syncing commands to {len(guild_ids)} specified guilds...")
#                for guild_id in guild_ids:
#                    try:
#                        guild = discord.Object(id=guild_id)
#                        await bot.tree.sync(guild=guild)
#                        log.info(f"Commands successfully synced to Guild ID: {guild_id}")
#                    except Exception as e:
#                        log.error(f"Failed to sync commands to Guild ID {guild_id}: {e}")
#            else:
#                log.warning("GUILD_IDS was set, but no valid IDs were found. Performing global sync.")
#                await bot.tree.sync()
#        else:
#            log.warning("GUILD_IDS environment variable not set. Performing global command sync.")
#            await bot.tree.sync()
#    
#        log.info("==================================================")
#        log.info(">>> Bot startup complete. Relay is now online! <<<")
#        log.info("==================================================")
#
#    # --- Automatic Cog Loading ---
#    log.info("Loading cogs...")
#    script_dir = os.path.dirname(os.path.abspath(__file__))
#    cogs_dir = os.path.join(script_dir, 'cogs')
#
#    for filename in os.listdir(cogs_dir):
#        if filename.endswith('.py') and not filename.startswith('__'):
#            try:
#                # The setup function in each cog will now find the services on the bot object.
#                await bot.load_extension(f'cogs.{filename[:-3]}')
#                log.info(f" -> Successfully loaded cog: {filename}")
#            except Exception as e:
#                log.error(f" -> Failed to load cog: {filename}", exc_info=e)
#    
#    # --- Run the Bot with Graceful Shutdown ---
#    try:
#        await bot.start(active_token)
#    finally:
#        # This block will run on any shutdown, clean or otherwise.
#        log.info("Closing database connection pool.")
#        await db_manager.close()#