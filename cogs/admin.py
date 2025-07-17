# cogs/admin.py

import os
import discord
import logging
from discord.ext import commands, tasks
from discord import app_commands
from typing import Optional
from core import UsageManager, DatabaseManager, GoogleProjectPoolManager

log = logging.getLogger(__name__)

@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
class AdminCog(commands.Cog, name="Admin"):
    """
    A cog for administrator-only commands and tasks.
    """
    def __init__(self, bot: commands.Bot, db_manager: DatabaseManager, usage_manager: UsageManager, gcp_pool_manager: GoogleProjectPoolManager):
        self.bot = bot
        self.db = db_manager
        self.usage = usage_manager
        self.gcp_pool = gcp_pool_manager
        
        # Start the background task if the usage manager is properly initialized
        if self.usage.is_initialized:
            self.sync_usage_task.start()

    def cog_unload(self):
        """Called when the cog is unloaded."""
        self.sync_usage_task.cancel()

    @tasks.loop(hours=1)
    async def sync_usage_task(self):
        """Periodically syncs the usage counter with Google's official data."""
        log.info("Running scheduled task: sync_usage_task")
        await self.usage.sync_with_google()

    @sync_usage_task.before_loop
    async def before_sync_usage_task(self):
        """Wait until the bot is ready before starting the task."""
        await self.bot.wait_until_ready()
        
    @app_commands.command(name="usage", description="Check the current API character usage for the month.")
    async def usage(self, interaction: discord.Interaction):
        """Displays detailed translation API usage across all projects."""
        if not self.usage.is_initialized or not self.gcp_pool.is_initialized:
            await interaction.response.send_message("The usage or GCP manager is not initialized.", ephemeral=True)
            return
            
        # Manually trigger a state load to ensure data is fresh, especially month rollover
        await self.usage._load_state()

        active_project = self.gcp_pool.get_active_project_details()['id']
        current_proj_usage = self.usage.characters_used_current_project
        rotation_limit = self.usage.rotation_threshold
        
        total_usage = self.usage.total_characters_used
        # Total limit is per-project limit * num projects
        total_limit = self.usage.safe_limit * len(self.gcp_pool.project_ids) if self.gcp_pool.project_ids else self.usage.safe_limit

        current_proj_percentage = (current_proj_usage / rotation_limit) * 100 if rotation_limit > 0 else 0
        total_percentage = (total_usage / total_limit) * 100 if total_limit > 0 else 0

        embed = discord.Embed(
            title="Translation API Usage",
            description=f"Data for month: `{self.usage.current_month}`",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Active Google Project",
            value=f"**`{active_project}`**",
            inline=False
        )
        embed.add_field(
            name="Current Project Usage (for rotation)",
            value=f"`{current_proj_usage:,}` / `{rotation_limit:,}` (`{current_proj_percentage:.2f}%)",
            inline=False
        )
        embed.add_field(
            name="Total Monthly Usage (All Projects)",
            value=f"`{total_usage:,}` / `{total_limit:,}` (`{total_percentage:.2f}%)",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="neon_status", description="Get a real-time status of the database connection.")
    async def neon_status(self, interaction: discord.Interaction):
        """Provides a real-time diagnostic of the database connection state."""
        if not self.db.is_initialized or not self.db.pool:
            await interaction.response.send_message("The database manager is not initialized.", ephemeral=True)
            return

        query = """
            SELECT
                state,
                usename as user,
                client_addr as client_address,
                now() - state_change AS duration
            FROM
                pg_stat_activity
            WHERE
                datname = current_database();
        """
        try:
            async with self.db.pool.acquire() as conn:
                records = await conn.fetch(query)

            if not records:
                await interaction.response.send_message("No active connections found for this database.", ephemeral=True)
                return

            embed = discord.Embed(title="Neon Database Connection Status", color=discord.Color.green())
            description = []
            for i, record in enumerate(records):
                description.append(f"**Connection {i+1}**")
                description.append(f"Status: `{record['state']}`")
                description.append(f"User: `{record['user']}`")
                description.append(f"Client: `{record['client_address']}`")
                description.append(f"Duration in State: `{record['duration']}`")
                description.append("-" * 20)
            
            embed.description = "\n".join(description)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except Exception as e:
            log.error(f"Failed to fetch Neon status: {e}", exc_info=True)
            await interaction.response.send_message(f"An error occurred while fetching DB status: `{e}`", ephemeral=True)

    @app_commands.command(name="set_guild_config", description="Configure channels for this guild (Admin only).")
    @app_commands.describe(
        onboarding_channel="The channel for new member onboarding.",
        admin_log_channel="The channel for bot administrative logs.",
        language_setup_role="The role given to new members to see the setup channel."
    )
    async def set_guild_config(
        self, 
        interaction: discord.Interaction, 
        onboarding_channel: Optional[discord.TextChannel] = None, 
        admin_log_channel: Optional[discord.TextChannel] = None,
        language_setup_role: Optional[discord.Role] = None
    ):
        """
        Sets the onboarding and admin log channels for the guild.
        """
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a guild.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild.id
        
        # Get IDs from the provided objects
        onboarding_cid = onboarding_channel.id if onboarding_channel else None
        admin_log_cid = admin_log_channel.id if admin_log_channel else None
        lang_role_id = language_setup_role.id if language_setup_role else None

        # Check if at least one parameter was provided
        if all(p is None for p in [onboarding_cid, admin_log_cid, lang_role_id]):
            await interaction.followup.send("Please provide at least one configuration option to set.", ephemeral=True)
            return

        try:
            await self.db.set_guild_config(
                guild_id=guild_id,
                onboarding_channel_id=onboarding_cid,
                admin_log_channel_id=admin_log_cid,
                language_setup_role_id=lang_role_id
            )
            
            response_parts = ["Guild configuration updated successfully!"]
            if onboarding_channel:
                response_parts.append(f"- Onboarding channel: {onboarding_channel.mention}")
            if admin_log_channel:
                response_parts.append(f"- Admin log channel: {admin_log_channel.mention}")
            if language_setup_role:
                response_parts.append(f"- Language setup role: {language_setup_role.mention}")
            
            await interaction.followup.send("\n".join(response_parts), ephemeral=True)

        except Exception as e:
            log.error(f"Error setting guild config in admin command for guild {guild_id}: {e}", exc_info=True)
            await interaction.followup.send("An error occurred while trying to set guild configuration.", ephemeral=True)

async def setup(bot: commands.Bot):
    """The setup function for the cog."""
    if not all(hasattr(bot, attr) for attr in ['db_manager', 'usage_manager', 'gcp_pool_manager']):
        log.critical("AdminCog cannot be loaded: Core services not found on bot object.")
        return
        
    await bot.add_cog(AdminCog(bot, bot.db_manager, bot.usage_manager, bot.gcp_pool_manager))