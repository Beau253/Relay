# cogs/admin.py

import os
import discord
import logging
import json # ADD THIS LINE
import asyncio
from discord.ext import commands, tasks
from discord import app_commands
from typing import Optional, List
import re # For parsing duration strings
from datetime import datetime, timedelta, timezone
from core import UsageManager, DatabaseManager, GoogleProjectPoolManager, language_autocomplete

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
        if self.usage and self.usage.is_initialized:
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
            log.error(f"Error setting guild config for guild {guild_id}: {e}", exc_info=True)
            await interaction.followup.send("An error occurred while trying to set guild configuration.", ephemeral=True)

    @app_commands.command(name="sync_members", description="Syncs all members, assigning the setup role to those who haven't set a language.")
    async def sync_members(self, interaction: discord.Interaction):
        """
        Manually syncs all members in the guild.
        - Adds the language setup role to members without a language preference in the DB.
        - Removes the language setup role from members who do have a preference set.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return
            
        guild = interaction.guild

        # 1. Get the guild's configuration from the database
        guild_config = await self.db.get_guild_config(guild.id)
        if not guild_config or not guild_config.get('language_setup_role_id'):
            await interaction.followup.send("The Language Setup Role is not configured for this server. Please use `/set_guild_config` first.", ephemeral=True)
            return

        setup_role_id = guild_config['language_setup_role_id']
        setup_role = guild.get_role(setup_role_id)
        if not setup_role:
            await interaction.followup.send(f"The configured setup role (ID: {setup_role_id}) could not be found. It may have been deleted.", ephemeral=True)
            return

        # 2. Initialize counters and fetch all non-bot members
        roles_added = 0
        roles_removed = 0
        members_to_check = [m for m in guild.members if not m.bot]
        total_members = len(members_to_check)
        
        log.info(f"Starting member sync for guild {guild.id}. Checking {total_members} members.")

        # 3. Loop through each member and apply logic
        last_update_time = asyncio.get_event_loop().time()

        for i, member in enumerate(members_to_check):
            # Non-blocking sleep to prevent hanging on one user
            await asyncio.sleep(0) 

            user_has_pref = await self.db.get_user_preferences(member.id)
            member_has_role = setup_role in member.roles

            # Case 1: Member needs the role
            if not user_has_pref and not member_has_role:
                try:
                    await member.add_roles(setup_role, reason="Admin manual sync")
                    roles_added += 1
                except discord.Forbidden:
                    log.warning(f"Could not add setup role to {member.display_name} ({member.id}) due to missing permissions.")
                except Exception as e:
                    log.error(f"Failed to add role to {member.id}: {e}")

            # Case 2: Member has completed setup and should not have the role
            elif user_has_pref and member_has_role:
                try:
                    await member.remove_roles(setup_role, reason="Admin manual sync - user already set language")
                    roles_removed += 1
                except discord.Forbidden:
                    log.warning(f"Could not remove setup role from {member.display_name} ({member.id}) due to missing permissions.")
                except Exception as e:
                    log.error(f"Failed to remove role from {member.id}: {e}")

            current_time = asyncio.get_event_loop().time()
            if current_time - last_update_time > 3:
                progress_percent = (i + 1) / total_members * 100
                await interaction.edit_original_response(content=f"⚙️ Syncing members... {i+1}/{total_members} ({progress_percent:.1f}%) complete.")
                last_update_time = current_time

        # 4. Report the final results
        log.info(f"Member sync complete for guild {guild.id}. Added: {roles_added}, Removed: {roles_removed}.")
        
        final_embed = discord.Embed(
            title="✅ Member Sync Complete",
            description=f"Checked **{total_members}** members.",
            color=discord.Color.green()
        )
        final_embed.add_field(name="Setup Roles Assigned", value=str(roles_added))
        final_embed.add_field(name="Setup Roles Removed", value=str(roles_removed))
        final_embed.set_footer(text="Members who need to set their language now have the setup role.")
        
        # Edit the original message one last time with the final embed report
        await interaction.edit_original_response(content="Sync finished!", embed=final_embed)

    @app_commands.command(name="set_hub_expiry", description="Sets or removes the expiration for a Translation Hub.")
    @app_commands.describe(duration="The duration until expiry (e.g., '30m', '2h', '7d') or 'permanent'.")
    async def set_hub_expiry(self, interaction: discord.Interaction, duration: str):
        """Admin command to change a hub's expiration."""
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.followup.send("This command must be run inside a hub's thread.", ephemeral=True)
            return

        hub_record = await self.db.get_hub_by_thread_id(interaction.channel.id)
        if not hub_record:
            await interaction.followup.send("This thread is not a registered Translation Hub.", ephemeral=True)
            return

        new_expiry: Optional[datetime] = None
        response_msg = ""
        duration_lower = duration.lower()

        if duration_lower == 'permanent':
            new_expiry = None
            response_msg = f"✅ This hub has been made **permanent** by {interaction.user.mention}."
        else:
            try:
                # Regex to parse strings like "10m", "2h", "5d"
                match = re.match(r"(\d+)\s*([mhd])", duration_lower)
                if not match:
                    raise ValueError("Invalid duration format.")
                
                value, unit = int(match.group(1)), match.group(2)
                if unit == 'm':
                    delta = timedelta(minutes=value)
                elif unit == 'h':
                    delta = timedelta(hours=value)
                elif unit == 'd':
                    delta = timedelta(days=value)
                
                new_expiry = datetime.now(timezone.utc) + delta
                response_msg = f"✅ This hub's expiration has been updated by {interaction.user.mention}. It will now expire at {discord.utils.format_dt(new_expiry, style='F')}."
            
            except (ValueError, TypeError):
                await interaction.followup.send("Invalid duration format. Use a number followed by 'm', 'h', or 'd' (e.g., `30m`, `2h`, `7d`), or `permanent`.", ephemeral=True)
                return

        updated = await self.db.update_hub_expiry(interaction.channel.id, new_expiry)
        if updated:
            await interaction.channel.send(response_msg)
            await interaction.followup.send("Hub expiration updated successfully.", ephemeral=True)
        else:
            await interaction.followup.send("Failed to update hub expiration in the database.", ephemeral=True)
            
    # --- Hub Management Command Group ---
    hub_admin = app_commands.Group(name="hub_admin", description="Admin commands for managing Translation Hubs.")

    @hub_admin.command(name="set_expiry", description="Sets or removes the expiration for an existing Translation Hub.")
    @app_commands.describe(duration="The duration until expiry (e.g., '30m', '2h', '7d') or 'permanent'.")
    async def hub_set_expiry(self, interaction: discord.Interaction, duration: str):
        # This command calls the main logic and must be run in the hub's thread.
        await self.set_hub_expiry(interaction, duration)

    # --- Auto-Translate Command Group ---
    autotranslate = app_commands.Group(name="autotranslate", description="Manage automatic channel translation.")

    @autotranslate.command(name="set", description="Enable auto-translation for a channel.")
    @app_commands.autocomplete(language=language_autocomplete)
    @app_commands.describe(
        channel="The channel to activate auto-translation in.",
        language="The language to translate messages INTO.",
        impersonate="Post translations using the original user's name and avatar (default: True).",
        delete_original_message="Delete the original message after translating (default: False)."
    )
    async def autotranslate_set(self, interaction: discord.Interaction, channel: discord.TextChannel, language: str, impersonate: bool = True, delete_original_message: bool = False):
        await self.db.set_auto_translate_channel(channel.id, interaction.guild_id, language, impersonate, delete_original_message)
        
        impersonate_status = "enabled" if impersonate else "disabled"
        delete_status = "enabled" if delete_original_message else "disabled"
        
        await interaction.response.send_message(
            f"✅ Auto-translation has been **enabled** for {channel.mention}.\n"
            f"- **Target Language:** `{language}`\n"
            f"- **Impersonation:** `{impersonate_status}`\n"
            f"- **Delete Original:** `{delete_status}`",
            ephemeral=True
        )

    @autotranslate.command(name="delete", description="Disable auto-translation for a channel.")
    @app_commands.describe(channel="The channel to disable auto-translation on.")
    async def autotranslate_delete(self, interaction: discord.Interaction, channel: str):
        try:
            channel_id = int(channel)
        except ValueError:
            await interaction.response.send_message("Invalid channel selected.", ephemeral=True)
            return
            
        channel_obj = self.bot.get_channel(channel_id)
        channel_mention = f"#{channel_obj.name}" if channel_obj else f"channel ID `{channel_id}`"

        await self.db.remove_auto_translate_channel(channel_id)
        await interaction.response.send_message(f"✅ Auto-translation has been **disabled** for {channel_mention}.", ephemeral=True)

    @autotranslate_delete.autocomplete('channel')
    async def autotranslate_delete_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        """Shows only channels with active auto-translate rules."""
        if not interaction.guild_id:
            return []
        
        configs = await self.db.get_all_auto_translate_configs_for_guild(interaction.guild_id)
        choices = []
        for config in configs:
            channel = self.bot.get_channel(config['channel_id'])
            if channel and current.lower() in channel.name.lower():
                choices.append(app_commands.Choice(name=f"#{channel.name}", value=str(channel.id)))
        
        return choices[:25]
    @autotranslate.command(name="list", description="List all channels with auto-translation enabled.")
    async def autotranslate_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild_id:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return
            
        configs = await self.db.get_all_auto_translate_configs_for_guild(interaction.guild_id)

        if not configs:
            await interaction.followup.send("No channels are configured for auto-translation in this server.", ephemeral=True)
            return

        embed = discord.Embed(title="Auto-Translate Configurations", color=discord.Color.blue())
        description = ""
        for config in configs:
            channel = self.bot.get_channel(config['channel_id'])
            channel_mention = channel.mention if channel else f"`Channel ID: {config['channel_id']}`"
            lang = config['target_language_code']
            impersonate_status = "✅" if config.get('impersonate', False) else "❌"
            delete_status = "✅" if config.get('delete_original', False) else "❌"
            description += f"{channel_mention} -> `{lang}` (Impersonate: {impersonate_status} | Delete Original: {delete_status})\n"
        
        embed.description = description
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- Server-Wide Auto-Translate Command Group ---
    server_translate = app_commands.Group(name="server_translate", description="Manage server-wide auto-translation.")

    @server_translate.command(name="set", description="Set a default translation language for all non-exempt channels.")
    @app_commands.autocomplete(language=language_autocomplete)
    @app_commands.describe(language="The language to translate all messages INTO by default.", impersonate="Post translations using the original user's name and avatar.", delete_original_message="Delete the original message after translating.")
    async def server_translate_set(self, interaction: discord.Interaction, language: str, impersonate: bool = False, delete_original_message: bool = False):
        if not interaction.guild_id: return
        await self.db.set_guild_config(guild_id=interaction.guild_id, server_wide_language=language, sw_impersonate=impersonate, sw_delete_original=delete_original_message)
        impersonate_status = "enabled" if impersonate else "disabled"
        delete_status = "enabled" if delete_original_message else "disabled"
        await interaction.response.send_message(f"✅ Server-wide auto-translation settings updated.\n- **Target Language:** `{language}`\n- **Impersonation:** `{impersonate_status}`\n- **Delete Original:** `{delete_status}`", ephemeral=True)

    @server_translate.command(name="disable", description="Disable the server-wide auto-translation rule.")
    async def server_translate_disable(self, interaction: discord.Interaction):
        if not interaction.guild_id: return
        await self.db.set_guild_config(guild_id=interaction.guild_id, server_wide_language=None)
        await interaction.response.send_message("✅ Server-wide auto-translation has been **disabled**.", ephemeral=True)

    @server_translate.command(name="exempt_add", description="Add a channel to the server-wide translation exemption list.")
    @app_commands.describe(channel="The channel to exempt from server-wide translation.")
    async def server_translate_exempt_add(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild_id: return
        await self.db.add_auto_translate_exemption(interaction.guild_id, channel.id)
        await interaction.response.send_message(f"✅ {channel.mention} is now **exempt** from server-wide auto-translation.", ephemeral=True)

    @server_translate.command(name="exempt_remove", description="Remove a channel from the exemption list.")
    @app_commands.describe(channel="The channel to remove from the exemption list.")
    async def server_translate_exempt_remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.remove_auto_translate_exemption(channel.id)
        await interaction.response.send_message(f"✅ {channel.mention} has been **removed** from the exemption list.", ephemeral=True)

    @server_translate.command(name="exempt_list", description="List all channels exempt from server-wide translation.")
    async def server_translate_exempt_list(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("This command must be run in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        exempt_channels = await self.db.get_exempt_channels(interaction.guild_id)
        if not exempt_channels:
            await interaction.followup.send("There are no channels on the exemption list for this server.", ephemeral=True)
            return
        embed = discord.Embed(title="Exempt Channels", color=discord.Color.orange())
        description = "These channels will be ignored by the server-wide auto-translator:\n\n"
        for record in exempt_channels:
            channel = self.bot.get_channel(record['channel_id'])
            channel_text = channel.mention if channel else f"`ID: {record['channel_id']}`"
            description += f"- {channel_text}\n"
        embed.description = description
        await interaction.followup.send(embed=embed, ephemeral=True)

    @server_translate.command(name="status", description="View the current server-wide translation settings.")
    async def server_translate_status(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("This command must be run in a server.", ephemeral=True)
            return
        guild_config = await self.db.get_guild_config(interaction.guild_id)
        embed = discord.Embed(title="Server-Wide Translation Status", color=discord.Color.blue())
        if guild_config and guild_config.get('server_wide_language'):
            lang = guild_config['server_wide_language']
            impersonate_status = "✅ Enabled" if guild_config.get('sw_impersonate', False) else "❌ Disabled"
            delete_status = "✅ Enabled" if guild_config.get('sw_delete_original', False) else "❌ Disabled"
            embed.description = f"Server-wide translation is **ENABLED**."
            embed.add_field(name="Target Language", value=f"`{lang}`", inline=False)
            embed.add_field(name="Impersonate Users", value=impersonate_status, inline=False)
            embed.add_field(name="Delete Originals", value=delete_status, inline=False)
        else:
            embed.description = "Server-wide translation is currently **DISABLED**."
            embed.color = discord.Color.red()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Glossary/Dictionary Command Group ---
    dictionary = app_commands.Group(name="dictionary", description="Manage the server's 'do-not-translate' dictionary.")

    @dictionary.command(name="add", description="Manually add a term to the dictionary.")
    @app_commands.describe(term="The word or phrase to protect from translation (case-insensitive).")
    async def dictionary_add(self, interaction: discord.Interaction, term: str):
        if not interaction.guild_id: return
        
        await self.db.add_glossary_term(interaction.guild_id, term)
        await interaction.response.send_message(f"✅ The term `{term}` has been added to the dictionary.", ephemeral=True)

    @dictionary.command(name="remove", description="Remove a term from the dictionary.")
    @app_commands.describe(term="The term to remove.")
    async def dictionary_remove(self, interaction: discord.Interaction, term: str):
        if not interaction.guild_id: return
        
        await self.db.remove_glossary_term(interaction.guild_id, term)
        await interaction.response.send_message(f"✅ The term `{term}` has been removed from the dictionary.", ephemeral=True)

    @dictionary.command(name="list", description="Lists all terms in the server's dictionary.")
    async def dictionary_list(self, interaction: discord.Interaction):
        if not interaction.guild_id: return
        await interaction.response.defer(ephemeral=True)
        
        terms = await self.db.get_glossary_terms(interaction.guild_id)
        if not terms:
            await interaction.followup.send("The dictionary for this server is currently empty.", ephemeral=True)
            return

        description = "\n".join(f"- `{term}`" for term in sorted(terms))
        
        embed = discord.Embed(
            title=f"Server Dictionary ({len(terms)} terms)",
            description=description,
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed)

    @dictionary.command(name="import", description="Import a pre-defined list of terms into the dictionary.")
    @app_commands.describe(pack="The name of the term pack to import.")
    @app_commands.choices(pack=[
        app_commands.Choice(name="General Slang", value="general_slang"),
        app_commands.Choice(name="Urban Dictionary Top 500", value="urban_slang"),
    ])
    async def dictionary_import(self, interaction: discord.Interaction, pack: app_commands.Choice[str]):
        if not interaction.guild_id: return
        await interaction.response.defer(ephemeral=True, thinking=True)

        pack_name = pack.value
        file_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'packs', f'{pack_name}.json')

        if not os.path.exists(file_path):
            await interaction.followup.send(f"Error: The data pack '{pack_name}' could not be found.", ephemeral=True)
            return
            
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                terms_to_import = json.load(f)
            
            if not isinstance(terms_to_import, list):
                await interaction.followup.send("Error: The data pack is not in the correct format (a list of strings).", ephemeral=True)
                return

            for term in terms_to_import:
                await self.db.add_glossary_term(interaction.guild_id, term)
            
            await interaction.followup.send(f"✅ Successfully imported the **{pack.name}** pack, adding up to **{len(terms_to_import)}** new terms to the dictionary.", ephemeral=True)

        except json.JSONDecodeError:
            await interaction.followup.send("Error: Failed to decode the data pack. It may be a corrupted JSON file.", ephemeral=True)
        except Exception as e:
            log.error(f"Error during dictionary import for guild {interaction.guild_id}: {e}", exc_info=True)
            await interaction.followup.send("An unexpected error occurred during the import process.", ephemeral=True)


async def setup(bot: commands.Bot):
    """The setup function for the cog."""
    if not all(hasattr(bot, attr) for attr in ['db_manager', 'usage_manager', 'gcp_pool_manager']):
        log.critical("AdminCog cannot be loaded: Core services not found on bot object.")
        return
    await bot.add_cog(AdminCog(bot, bot.db_manager, bot.usage_manager, bot.gcp_pool_manager))