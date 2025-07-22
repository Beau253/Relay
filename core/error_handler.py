# core/error_handler.py

import os
import traceback
import logging
import discord
from discord.app_commands import AppCommandError
from typing import Optional # ADDED: Required for Optional type hint

# Import DatabaseManager to access guild configurations
from core import DatabaseManager # Ensure core/__init__.py exposes DatabaseManager

log = logging.getLogger(__name__)

# Fetch the bot's operating mode from the environment variables.
BOT_MODE = os.getenv('BOT_MODE', 'development')

OWNER_ID = os.getenv('OWNER_ID')

async def send_error_report(interaction: discord.Interaction, error: AppCommandError):
    """
    Handles slash command errors by sending a user-friendly message (generic in production,
    or a specific one based on BOT_MODE logic here), and logs a detailed report
    to the console and an admin channel (if configured).
    """
    # When discord.py wraps an error in AppCommandError, the original exception
    # is stored in the 'original' attribute. We prioritize this for the real traceback.
    original_error = getattr(error, 'original', error)
    
    # Log the full, un-truncated error to the console for our records.
    # This happens regardless of bot mode or admin channel configuration.
    log.error(
        f"Exception caught in command `/{interaction.command.name}` "
        f"in guild `{interaction.guild.name if interaction.guild else 'DM'}` "
        f"by user `{interaction.user.name}` ({interaction.user.id})",
        exc_info=original_error # This logs the full traceback
    )

    # --- Send Error Message to User (Always Ephemeral, content varies by BOT_MODE) ---
    user_message = "Sorry, an unexpected error occurred."
    if BOT_MODE == 'production':
        user_message = "Sorry, an unexpected error occurred. The developers have been notified."
    # In development mode, the default 'user_message' "Sorry, an unexpected error occurred." is used for the user.
    # The detailed traceback for dev mode is sent ONLY to the admin log channel.

    try:
        # If the bot has already responded (e.g., with defer()), we must use followup.
        if interaction.response.is_done():
            await interaction.followup.send(user_message, ephemeral=True)
        else:
            await interaction.response.send_message(user_message, ephemeral=True)
    except discord.errors.NotFound:
        log.warning("Could not send user error message: interaction expired or invalid.")
    except Exception as e:
        log.error(f"Failed to send user-facing error message to Discord: {e}", exc_info=True)

    if interaction.guild: # Only attempt to log to admin channel if the interaction originated in a guild
        try:
            # Safely get the DatabaseManager instance from the bot
            db_manager: Optional[DatabaseManager] = getattr(interaction.client, 'db_manager', None)
            
            if db_manager and db_manager.is_initialized:
                guild_config = await db_manager.get_guild_config(interaction.guild.id)
                admin_log_channel_id = None
                if guild_config:
                    admin_log_channel_id = guild_config['admin_log_channel_id']

                if admin_log_channel_id:
                    admin_log_channel = interaction.client.get_channel(admin_log_channel_id)
                    if admin_log_channel and isinstance(admin_log_channel, discord.TextChannel):
                        error_embed = discord.Embed(
                            title=f"Command Error: `/{interaction.command.name}`",
                            description=f"**User:** {interaction.user.mention}\n**Channel:** {interaction.channel.mention}\n**Error:** `{type(original_error).__name__}: {original_error}`",
                            color=discord.Color.red()
                        )
                        # Add traceback to embed description in dev mode for admin channel
                        if BOT_MODE == 'development':
                            traceback_str = "".join(traceback.format_exception(type(original_error), original_error, original_error.__traceback__))
                            if len(traceback_str) > 3900: # Slightly less than 4096 to account for other text
                                traceback_str = f"{traceback_str[:3900]}... (truncated)"
                            error_embed.add_field(name="Traceback", value=f"```py\n{traceback_str}\n```", inline=False)
                            error_embed.set_footer(text="Full traceback also in bot console logs.")

                        ping_content = f"<@{OWNER_ID}>" if OWNER_ID else ""

                        # MODIFIED: Send the ping content along with the embed
                        await admin_log_channel.send(content=ping_content, embed=error_embed)
                        
                        await admin_log_channel.send(embed=error_embed)
                        log.info(f"Error report sent to admin log channel {admin_log_channel_id} in guild {interaction.guild.id}.")
                    else:
                        log.warning(f"Configured admin log channel {admin_log_channel_id} for guild {interaction.guild.id} is invalid or not a text channel. Error only logged to console.")
                else:
                    log.info(f"No admin log channel configured for guild {interaction.guild.id}. Error only logged to console.")
            else:
                log.warning("DatabaseManager not available or initialized for logging error to admin channel. Error only logged to console.")
        except Exception as e:
            log.error(f"Failed to send error report to admin channel for guild {interaction.guild.id}: {e}", exc_info=True)
    else:
        log.info("Error occurred outside a guild context (e.g., DM). Error only logged to console.")
