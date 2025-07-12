# cogs/onboarding.py

import os
import asyncio
import discord
import logging
from discord.ext import commands
from discord import app_commands

# Import our core services
from core import DatabaseManager

log = logging.getLogger(__name__)

# This is the persistent View that holds our button.
# It needs to be defined at the top level so the bot can re-register it on startup.
class OnboardingView(discord.ui.View):
    def __init__(self, db_manager: DatabaseManager):
        # The timeout is set to None to make this view persistent.
        super().__init__(timeout=None)
        self.db = db_manager

    @discord.ui.button(label="Set My Language", style=discord.ButtonStyle.primary, custom_id="onboarding:set_language_button")
    async def set_language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        This callback is executed when a user clicks the button.
        """
        # Acknowledge the interaction quickly and privately.
        await interaction.response.defer(ephemeral=True)

        user_locale = str(interaction.locale)
        user_id = interaction.user.id

        try:
            # 1. Save the preference to the database.
            await self.db.set_user_preferences(user_id=user_id, user_locale=user_locale)
            log.info(f"Onboarding: User {user_id} set locale to '{user_locale}'.")

            # 2. Respond with a private confirmation message.
            await interaction.followup.send(
                f"Thank you! Your preferred language has been set to `{user_locale}` based on your Discord client settings.", ephemeral=True
            )

            # 3. Start the background task to remove the user from the channel.
            asyncio.create_task(self.remove_user_from_channel(interaction.user, interaction.channel))

        except Exception as e:
            log.error(f"Error during onboarding interaction for user {user_id}: {e}", exc_info=True)
            await interaction.followup.send("An unexpected error occurred. Please try again later.")

    async def remove_user_from_channel(self, member: discord.Member, channel: discord.TextChannel):
        """Waits for a short period then removes the user's access to the channel."""
        await asyncio.sleep(60) # 60-second grace period
        try:
            await channel.set_permissions(member, view_channel=False)
            log.info(f"Onboarding: Removed user {member.id} from channel {channel.id}.")
        except discord.errors.Forbidden:
            log.error(f"Failed to remove permissions for {member.id} in {channel.id}. Missing 'Manage Permissions'?")
        except Exception as e:
            log.error(f"Error removing user {member.id} from onboarding channel: {e}", exc_info=True)


@app_commands.guild_only()
class OnboardingCog(commands.Cog, name="Onboarding"):
    """Handles the new member onboarding process."""
    def __init__(self, bot: commands.Bot, db_manager: DatabaseManager):
        self.bot = bot
        self.db = db_manager
        self.onboarding_channel_id = int(os.getenv("ONBOARDING_CHANNEL_ID", 0))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Grants a new member access to the private onboarding channel based on guild config."""
        if member.bot:
            return # Ignore other bots

        if not member.guild:
            log.warning(f"Member {member.id} joined without a guild context. Skipping onboarding.")
            return

        guild_id = member.guild.id
        guild_config = await self.db.get_guild_config(guild_id)
        
        onboarding_channel_id = None
        if guild_config:
            onboarding_channel_id = guild_config['onboarding_channel_id']
        
        if not onboarding_channel_id:
            log.info(f"No onboarding channel configured for guild {guild_id}. Skipping welcome message.")
            return # No onboarding channel set for this guild

        channel = self.bot.get_channel(onboarding_channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            log.warning(f"Configured onboarding channel {onboarding_channel_id} for guild {guild_id} is invalid or not a text channel.")
            return
        
        log.info(f"New member joined: {member.name} ({member.id}). Granting access to onboarding channel.")
        try:
            await channel.set_permissions(member, view_channel=True)
            # You might want to send the welcome message here as well, if it's a fixed welcome message
            # that doesn't require setup_onboarding to be run.
            # Example: await channel.send(f"Welcome {member.mention}! Please click the button below...")
        except discord.errors.Forbidden:
            log.error(f"Failed to set permissions for {member.id} in {channel.name} ({channel.id}) for guild {member.guild.name}. Bot may be missing 'Manage Permissions'.")
        except Exception as e:
            log.error(f"Error setting permissions for new member {member.id}: {e}", exc_info=True)

    @app_commands.command(name="setup_onboarding", description="Posts the persistent onboarding message.")
    @app_commands.default_permissions(administrator=True)
    async def setup_onboarding(self, interaction: discord.Interaction):
        """Admin command to post the welcome message with the persistent button."""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a guild.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        guild_config = await self.db.get_guild_config(guild_id)
        onboarding_channel_id = None
        if guild_config:
            onboarding_channel_id = guild_config['onboarding_channel_id']

        if not onboarding_channel_id:
            await interaction.response.send_message(
                "No onboarding channel is configured for this guild. Please use `/set_guild_config` first.",
                ephemeral=True
            )
            return

        if interaction.channel.id != onboarding_channel_id:
            await interaction.response.send_message(
                f"This command can only be used in the designated onboarding channel (<#{onboarding_channel_id}>).",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="Welcome & Language Setup",
            description=(
                "Welcome to the server!\n\n"
                "To ensure you have the best experience, please click the button below. "
                "This will allow our bot to know your preferred language and provide "
                "on-demand translations for you throughout the server."
            ),
            color=discord.Color.blurple()
        )
        # We pass the db_manager instance to the view when we create it.
        view = OnboardingView(self.db)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("Onboarding message has been posted.", ephemeral=True)


async def setup(bot: commands.Bot):
    """The setup function for the cog."""
    if not hasattr(bot, 'db_manager'):
        log.critical("OnboardingCog cannot be loaded: DatabaseManager not found on bot object.")
        return
        
    # Crucially, we add the persistent view to the bot *before* adding the cog.
    # This ensures the button works even after the bot restarts.
    bot.add_view(OnboardingView(bot.db_manager))
    
    await bot.add_cog(OnboardingCog(bot, bot.db_manager))