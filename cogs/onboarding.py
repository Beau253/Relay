# cogs/onboarding.py

import discord
import logging
from discord.ext import commands
from discord import app_commands

from core import DatabaseManager

log = logging.getLogger(__name__)

# The role name we will create if one isn't configured.
SETUP_ROLE_NAME = "Language Setup"

# --- Onboarding View (Button Logic) ---
class OnboardingView(discord.ui.View):
    def __init__(self, db_manager: DatabaseManager):
        super().__init__(timeout=None)
        self.db = db_manager

    @discord.ui.button(label="Set My Language", style=discord.ButtonStyle.primary, custom_id="onboarding:set_language_button")
    async def set_language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            # 1. Save language preference to DB
            await self.db.set_user_preferences(user_id=interaction.user.id, user_locale=str(interaction.locale))
            log.info(f"Onboarding: User {interaction.user.id} set locale to '{interaction.locale}'.")

            # 2. Find and remove the setup role
            guild_config = await self.db.get_guild_config(interaction.guild_id)
            if guild_config and guild_config.get('language_setup_role_id'):
                role_id = guild_config['language_setup_role_id']
                setup_role = interaction.guild.get_role(role_id)
                if setup_role and isinstance(interaction.user, discord.Member) and setup_role in interaction.user.roles:
                    await interaction.user.remove_roles(setup_role, reason="User completed language setup.")
                    log.info(f"Removed '{setup_role.name}' role from user {interaction.user.id}.")

            # 3. Respond with confirmation
            await interaction.followup.send(f"Thank you! Your preferred language is set to `{interaction.locale}`. You can now access the rest of the server.", ephemeral=True)

        except Exception as e:
            log.error(f"Error during onboarding interaction for user {interaction.user.id}: {e}", exc_info=True)
            await interaction.followup.send("An unexpected error occurred. Please try again later.", ephemeral=True)

# --- Onboarding Cog (Role Assignment Logic) ---
@app_commands.guild_only()
class OnboardingCog(commands.Cog, name="Onboarding"):
    def __init__(self, bot: commands.Bot, db_manager: DatabaseManager):
        self.bot = bot
        self.db = db_manager

    async def _get_or_create_setup_role(self, guild: discord.Guild) -> discord.Role | None:
        """Gets the configured setup role, or creates it if it doesn't exist."""
        guild_config = await self.db.get_guild_config(guild.id)
        role_id = guild_config.get('language_setup_role_id') if guild_config else None

        # Try to find the role by ID if it's configured
        if role_id:
            role = guild.get_role(role_id)
            if role:
                return role
            else:
                log.warning(f"Language setup role ID {role_id} configured but not found in guild {guild.id}. Will create a new one.")

        # If not configured or not found, try to find by name
        role = discord.utils.get(guild.roles, name=SETUP_ROLE_NAME)
        if role:
            log.info(f"Found existing role '{SETUP_ROLE_NAME}'. Saving its ID to config.")
            await self.db.set_guild_config(guild_id=guild.id, language_setup_role_id=role.id)
            return role
        
        # If it doesn't exist at all, create it
        try:
            log.info(f"Creating new role '{SETUP_ROLE_NAME}' in guild {guild.id}.")
            new_role = await guild.create_role(name=SETUP_ROLE_NAME, reason="Initial setup for language selection role.")
            await self.db.set_guild_config(guild_id=guild.id, language_setup_role_id=new_role.id)
            return new_role
        except discord.Forbidden:
            log.error(f"Bot lacks 'Manage Roles' permission in guild {guild.id} to create the setup role.")
            return None
        except Exception as e:
            log.error(f"Failed to create setup role in guild {guild.id}: {e}", exc_info=True)
            return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot: return

        guild_config = await self.db.get_guild_config(member.guild.id)
        if not guild_config or not guild_config.get('onboarding_channel_id'):
            log.info(f"No onboarding channel configured for guild {member.guild.id}. Skipping role assignment.")
            return
        
        onboarding_channel = self.bot.get_channel(guild_config['onboarding_channel_id'])
        if not onboarding_channel: return
            
        # Get the role, creating it if necessary
        setup_role = await self._get_or_create_setup_role(member.guild)
        if not setup_role:
            log.error(f"Could not get or create setup role for guild {member.guild.id}. Cannot assign to new member.")
            return

        try:
            # Add the role to the new member
            await member.add_roles(setup_role, reason="New member joining.")
            log.info(f"Assigned '{setup_role.name}' role to new member {member.id}.")
            # Also ensure the role has permissions to see the channel
            await onboarding_channel.set_permissions(setup_role, view_channel=True)
        except discord.Forbidden:
            log.error(f"Failed to assign role to {member.id} in guild {member.guild.id}. Bot may be missing 'Manage Roles' permission or have a lower role.")
        except Exception as e:
            log.error(f"Error assigning role to new member {member.id}: {e}", exc_info=True)

    @app_commands.command(name="setup_onboarding", description="Posts the persistent onboarding message.")
    @app_commands.default_permissions(administrator=True)
    async def setup_onboarding(self, interaction: discord.Interaction):
        if not interaction.guild: return
        guild_config = await self.db.get_guild_config(interaction.guild.id)
        onboarding_channel_id = guild_config.get('onboarding_channel_id') if guild_config else None

        if not onboarding_channel_id:
            await interaction.response.send_message("No onboarding channel is configured. Use `/set_guild_config` first.", ephemeral=True)
            return

        if interaction.channel.id != onboarding_channel_id:
            await interaction.response.send_message(f"This command must be used in the designated onboarding channel (<#{onboarding_channel_id}>).", ephemeral=True)
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
        view = OnboardingView(self.db)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("Onboarding message has been posted.", ephemeral=True)


async def setup(bot: commands.Bot):
    if not hasattr(bot, 'db_manager'):
        log.critical("OnboardingCog cannot be loaded: DatabaseManager not found on bot object.")
        return
    bot.add_view(OnboardingView(bot.db_manager))
    await bot.add_cog(OnboardingCog(bot, bot.db_manager))