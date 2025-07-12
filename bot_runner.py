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


# --- Main Bot Runner Function ---
async def run_bot():
    """
    Initializes and runs the Discord bot.
    """
    # --- Initialize Core Services (in dependency order) ---
    # 1. Services with no dependencies
    db_manager = DatabaseManager()
    translator = TextTranslator()

    # 2. Services that depend on the above
    gcp_pool_manager = GoogleProjectPoolManager(db_manager)
    usage_manager = UsageManager(db_manager, gcp_pool_manager)
    bot_pool_manager = BotPoolManager(db_manager, usage_manager)

    # --- Await Initialization (in dependency order) ---
    await db_manager.initialize()
    await gcp_pool_manager.initialize(translator) # Must be initialized before usage_manager
    await usage_manager.initialize()
    await bot_pool_manager.initialize()

    # --- Get Active Token from the Pool Manager ---
    try:
        active_token = await bot_pool_manager.get_active_token()
    except ShutdownForBotRotation as e:
        log.warning(f"Shutdown signal received during startup: {e}")
        # Re-raise to be caught by app.py for a clean exit
        raise e
    
    if not active_token:
        log.critical("FATAL: No active token could be determined by the BotPoolManager.")
        return

    # Define the intents required for the bot's features.
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.reactions = True

    # Create the bot instance.
    bot = commands.Bot(command_prefix="!", intents=intents)
    
    localizer = BotLocalizer()
    await bot.tree.set_translator(localizer)
    
    # --- Attach Core Services to the Bot Instance ---
    bot.db_manager = db_manager
    bot.translator = translator
    bot.usage_manager = usage_manager
    bot.bot_pool_manager = bot_pool_manager
    bot.gcp_pool_manager = gcp_pool_manager

    # --- Register the Global Error Handler ---
    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        await send_error_report(interaction, error)

    # --- Bot Startup Event ---
    @bot.event
    async def on_ready():
        log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        log.info(f"Version: {BOT_VERSION} | Mode: {BOT_MODE}")
        log.info("--------------------------------------------------")
        
        guild_ids_str = os.getenv("GUILD_IDS")
        if guild_ids_str:
            # Parse the environment variable into a list of integers
            guild_ids = [int(gid.strip()) for gid in guild_ids_str.split(',') if gid.strip().isdigit()]
    
            if guild_ids:
                log.info(f"Syncing commands to {len(guild_ids)} specified guilds...")
                # Create a list of discord.Object for each guild ID
                guild_objects = [discord.Object(id=gid) for gid in guild_ids]
        
                # Sync the application command tree to the specified guilds.
                # This is the correct way to do a guild-specific sync.
                # It does not copy commands; it just tells Discord where to register them.
                await bot.tree.sync(guilds=guild_objects)
        
                log.info(f"Successfully synced commands to guilds: {guild_ids}")
            else:
                log.warning("GUILD_IDS was set, but no valid IDs were found. Performing global sync.")
                await bot.tree.sync() # Fallback to global sync
        else:
                # If GUILD_IDS is not set at all, perform a global sync.
            log.warning("GUILD_IDS environment variable not set. Performing global command sync (may take up to an hour).")
            await bot.tree.sync()

        log.info("==================================================")
        log.info(">>> Bot startup complete. Relay is now online! <<<")
        log.info("==================================================")

    # --- Automatic Cog Loading ---
    log.info("Loading cogs...")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cogs_dir = os.path.join(script_dir, 'cogs')

    for filename in os.listdir(cogs_dir):
        if filename.endswith('.py') and not filename.startswith('__'):
            try:
                # The setup function in each cog will now find the services on the bot object.
                await bot.load_extension(f'cogs.{filename[:-3]}')
                log.info(f" -> Successfully loaded cog: {filename}")
            except Exception as e:
                log.error(f" -> Failed to load cog: {filename}", exc_info=e)
    
    # --- Run the Bot with Graceful Shutdown ---
    try:
        await bot.start(active_token)
    finally:
        # This block will run on any shutdown, clean or otherwise.
        log.info("Closing database connection pool.")
        await db_manager.close()