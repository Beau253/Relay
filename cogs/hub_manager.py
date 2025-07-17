# cogs/hub_manager.py

import os
import discord
import logging
import asyncpg
import json
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict
from core import language_autocomplete, SUPPORTED_LANGUAGES
from core import DatabaseManager, TextTranslator, UsageManager

log = logging.getLogger(__name__)

MAIN_LANGUAGE = 'en'

LANG_TO_COUNTRY_CODE = {
    'en': 'GB', 'es': 'ES', 'fr': 'FR', 'de': 'DE', 'it': 'IT', 'pt': 'PT',
    'ru': 'RU', 'zh': 'CN', 'zh-TW': 'TW', 'yue': 'HK', 'ja': 'JP',
    'ko': 'KR', 'ar': 'SA', 'hi': 'IN', 'id': 'ID', 'ms': 'MY',
    'vi': 'VN', 'ur': 'PK', 'nl': 'NL', 'sv': 'SE', 'no': 'NO',
    'da': 'DK', 'fi': 'FI', 'pl': 'PL', 'tr': 'TR'
}

def country_code_to_flag(code: str) -> str:
    """Converts a two-letter country code (e.g., 'US') to a flag emoji (e.g., '🇺🇸')."""
    # The offset between the uppercase letter 'A' and the Regional Indicator Symbol 'A'
    OFFSET = 0x1F1E6 - ord('A')
    
    # Return a default white flag if the code is invalid.
    if not code or len(code) != 2:
        return '🏳️'
    
    code = code.upper()
    # Combine the two regional indicator characters to form the flag.
    return chr(ord(code[0]) + OFFSET) + chr(ord(code[1]) + OFFSET)

MAIN_LANGUAGE_COUNTRY_CODE = LANG_TO_COUNTRY_CODE.get(MAIN_LANGUAGE, 'US')
MAIN_LANGUAGE_FLAG = country_code_to_flag(MAIN_LANGUAGE_COUNTRY_CODE)

class UITranslator:
    def __init__(self):
        self.translations = {}
        locale_dir = 'locale'
        if os.path.isdir(locale_dir):
            for filename in os.listdir(locale_dir):
                if filename.endswith('.json'):
                    lang_code = filename[:-5]
                    with open(os.path.join(locale_dir, filename), 'r', encoding='utf-8') as f:
                        try:
                            self.translations[lang_code] = json.load(f)
                        except json.JSONDecodeError as e:
                            log.error(f"JSON Decode Error in {filename}: {e}")
                        except Exception as e:
                            log.error(f"Error loading {filename}: {e}")

    def get_string(self, key: str, locale: str, **kwargs) -> str:
        """Gets a translated string from the loaded files, with fallback to English."""
        translated = self.translations.get(locale, {}).get(key)
        if translated:
            return translated.format(**kwargs)
        
        base_lang = locale.split('-')[0]
        translated = self.translations.get(base_lang, {}).get(key)
        if translated:
            return translated.format(**kwargs)
            
        translated = self.translations.get('en', {}).get(key, key)
        return translated.format(**kwargs)

# Create a single instance of the UI translator to be used by the cog.
ui_translator = UITranslator()

# --- UI Components for Hub Extension ---
class HubExtensionView(discord.ui.View):
    """A view with a dropdown and button to extend a hub's session."""
    def __init__(self, target_lang: str):
        # timeout=None makes this view persistent, so it works even after the bot restarts.
        super().__init__(timeout=None)
        self.target_lang = target_lang
        # This state variable is crucial. It stores the value from the dropdown.
        self.selected_duration: Optional[int] = None

    @classmethod
    async def create(cls, db: DatabaseManager, target_lang: str):
        """A factory method to asynchronously create and configure the view with localized text."""
        view = cls(target_lang)
        
        # Fetch all UI strings from our local files using the UITranslator.
        select_placeholder = ui_translator.get_string("HubUI-ExtendPlaceholder", view.target_lang)
        extend_button_label = ui_translator.get_string("HubUI-ExtendButton", view.target_lang)

        # Create options with translated labels for the dropdown.
        options = [
            discord.SelectOption(label=ui_translator.get_string("HubUI-Duration5m", view.target_lang), value="5"),
            discord.SelectOption(label=ui_translator.get_string("HubUI-Duration15m", view.target_lang), value="15"),
            discord.SelectOption(label=ui_translator.get_string("HubUI-Duration30m", view.target_lang), value="30"),
            discord.SelectOption(label=ui_translator.get_string("HubUI-Duration1h", view.target_lang), value="60")
        ]
        
        select_menu = discord.ui.Select(custom_id="hub:duration_select", placeholder=select_placeholder, options=options)
        extend_button = discord.ui.Button(label=extend_button_label, style=discord.ButtonStyle.success, custom_id="hub:extend_button")

        # Assign callbacks to the components.
        select_menu.callback = view.duration_select_callback
        extend_button.callback = view.extend_button_callback
        
        # Attach the database manager to the view instance for use in the button callback.
        view.db = db
        
        view.add_item(select_menu)
        view.add_item(extend_button)
        return view

    async def duration_select_callback(self, interaction: discord.Interaction):
        """This callback is fired when a user selects a duration from the dropdown."""
        # Store the selected value in our state variable. This is the robust way to handle state.
        self.selected_duration = int(interaction.data['values'][0])
        # Defer the response to acknowledge the selection without sending a message.
        await interaction.response.defer()

    async def extend_button_callback(self, interaction: discord.Interaction):
        """This callback is fired when the user clicks the 'Extend Session' button."""
        # Check our state variable. This is more reliable than trying to read from interaction data.
        if self.selected_duration is None:
            error_msg = ui_translator.get_string("HubUI-ErrorSelectFirst", self.target_lang)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        minutes_to_add = self.selected_duration
        new_expiry_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_add)

        # Update the hub's expiry time in the database.
        updated = await self.db.update_hub_expiry(interaction.channel.id, new_expiry_time)
        
        if updated:
            log.info(f"Hub {interaction.channel.id} extended by {minutes_to_add} minutes by user {interaction.user.id}")
            
            expiry_formatted = discord.utils.format_dt(new_expiry_time, style='F')
            confirmation_msg = ui_translator.get_string("HubUI-ConfirmExtended", self.target_lang, expiry_time=expiry_formatted)
            
            # Respond to the interaction and delete the original message with the button.
            await interaction.response.send_message(confirmation_msg)
            await interaction.message.delete()
        else:
            # This happens if the hub was already archived/deleted before the button was clicked.
            error_msg = ui_translator.get_string("HubUI-ErrorExpired", self.target_lang)
            await interaction.response.send_message(error_msg, ephemeral=True)

@app_commands.guild_only()
class HubManagerCog(commands.Cog, name="Hub Manager"):
    """Manages the creation, synchronization, and lifecycle of Live Translation Hubs."""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, translator: TextTranslator, usage: UsageManager):
        self.bot = bot
        self.db = db
        self.translator = translator
        self.usage = usage
        self.webhook_cache: Dict[int, discord.Webhook] = {}        
        
        # Start all background tasks
        self.check_hubs_for_warnings.start()
        self.check_hubs_for_expiration.start()

        log.info("[HUB_MANAGER_COG] Initializing and adding 'Translate this Channel' context menu...")
        self.translate_channel_menu = app_commands.ContextMenu(
            name='Translate this Channel',
            callback=self.translate_channel_callback,
        )
        self.bot.tree.add_command(self.translate_channel_menu)
        log.info("[HUB_MANAGER_COG] 'Translate this Channel' context menu added to tree.")

    def cog_unload(self):
        self.check_hubs_for_warnings.cancel()
        self.check_hubs_for_expiration.cancel()

    # --- LOCALIZATION AND WEBHOOK HELPERS ---
    async def _send_localized_hub_message(self, thread: discord.Thread, target_lang: str, english_text: str, view: Optional[discord.ui.View] = None):
        """Translates a message and sends it to a hub. Falls back to English on failure."""
        translated_text = await self.translator.translate_text(english_text, target_lang)
        if translated_text:
            await self.usage.record_usage(len(english_text))
        else:
            translated_text = english_text
        await thread.send(translated_text, view=view)

    async def _get_webhook(self, channel: discord.TextChannel | discord.Thread) -> Optional[discord.Webhook]:
        target_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        if target_channel.id in self.webhook_cache:
            return self.webhook_cache[target_channel.id]
        try:
            webhooks = await target_channel.webhooks()
            webhook = discord.utils.get(webhooks, name="Relay Translator")
            if webhook is None:
                log.info(f"Creating new webhook in channel #{target_channel.name}")
                webhook = await target_channel.create_webhook(name="Relay Translator")
            self.webhook_cache[target_channel.id] = webhook
            return webhook
        except discord.Forbidden:
            log.error(f"Missing 'Manage Webhooks' permission in channel #{target_channel.name}")
            return None

    async def _send_webhook_message(self, channel: discord.TextChannel | discord.Thread, content: str, author: discord.Member | discord.User, custom_username: Optional[str] = None):
        webhook = await self._get_webhook(channel)
        if not webhook: return
        
        username_to_use = custom_username if custom_username is not None else author.display_name

        try:
            if isinstance(channel, discord.Thread):
                await webhook.send(content=content, username=username_to_use, avatar_url=author.display_avatar.url, thread=channel)
            else:
                await webhook.send(content=content, username=username_to_use, avatar_url=author.display_avatar.url)
        except (discord.Forbidden, discord.NotFound) as e:
            log.error(f"Failed to send webhook message to {channel.id}: {e}")

    # --- HUB LIFECYCLE TASKS ---

    @tasks.loop(minutes=1)
    async def check_hubs_for_warnings(self):
        """Posts a warning message in hubs that are nearing expiration."""
        if not self.db.is_initialized: return
        hubs_to_warn = await self.db.get_hubs_needing_warning()
        for hub_record in hubs_to_warn:
            thread = self.bot.get_channel(hub_record['thread_id'])
            if thread and isinstance(thread, discord.Thread):
                log.info(f"Hub {thread.id} is nearing expiration. Posting warning.")
                lang_code = hub_record['language_code']
                
                view = await HubExtensionView.create(self.db, lang_code)
                warning_template = "**This translation session is about to expire.** Please select a duration and click Extend to keep it active."
                await self._send_localized_hub_message(thread, lang_code, warning_template, view=view)
                
                await self.db.mark_hub_warning_sent(thread.id)

    @check_hubs_for_warnings.before_loop
    async def before_check_hubs_for_warnings(self):
        """Wait until the bot is ready before starting the task."""
        await self.bot.wait_until_ready()
        log.info("HubManagerCog: 'check_hubs_for_warnings' loop is ready.")

    @tasks.loop(minutes=1)
    async def check_hubs_for_expiration(self):
        """Archives expired hubs after a grace period."""
        # This task now checks for hubs that expired 5 minutes ago to create a grace period
        five_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        query = "SELECT * FROM translation_hubs WHERE expires_at < $1 AND is_archived = FALSE;"
        expired_hubs = await self.db.pool.fetch(query, five_mins_ago)
        for hub_record in expired_hubs:
            thread_id = hub_record['thread_id']
            try:
                thread = await self.bot.fetch_channel(thread_id)
                if isinstance(thread, discord.Thread):
                    log.info(f"Hub '{thread.name}' ({thread_id}) has passed grace period. Archiving.")
                    expiration_template = "This translation hub has expired and is now archived."
                    await self._send_localized_hub_message(thread, hub_record['language_code'], expiration_template)
                    await thread.edit(archived=True, locked=True)
                await self.db.archive_hub(thread_id)
            except discord.NotFound:
                log.warning(f"Could not find expired thread {thread_id} to archive. Marking as archived anyway.")
            except Exception as e:
                log.error(f"Error during hub archival for thread {thread_id}: {e}", exc_info=True)
    
    async def create_hub_logic(self, interaction: discord.Interaction, language: str, channel: discord.TextChannel):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        if language.lower().startswith('en'):
            await interaction.followup.send(
                "❌ You cannot create a translation hub for English, as it is the main language of the server.", 
                ephemeral=True
            )
            return

        if language not in SUPPORTED_LANGUAGES:
            await interaction.followup.send(f"Sorry, '{language}' is not a supported language code.", ephemeral=True)
            return
            
        active_hub_record = await self.db.get_active_hub(channel.id, language)
        if active_hub_record:
            try:
                thread = self.bot.get_channel(active_hub_record['thread_id']) or await self.bot.fetch_channel(active_hub_record['thread_id'])
                await interaction.followup.send(f"A hub for `{language}` already exists for this channel here: {thread.mention}", ephemeral=True)
                return
            except discord.NotFound:
                log.warning(f"Found stale active hub record for a deleted thread ({active_hub_record['thread_id']}). Deleting record and proceeding.")
                await self.db.delete_hub(active_hub_record['thread_id'])
        
        archived_hub_record = await self.db.get_archived_hub(channel.id, language)
        if archived_hub_record:
            try:
                # Find the archived thread
                thread = await self.bot.fetch_channel(archived_hub_record['thread_id'])
                if isinstance(thread, discord.Thread):
                    log.info(f"Reactivating archived hub {thread.id} for user {interaction.user.id}")
                    # Unarchive and unlock the thread
                    await thread.edit(archived=False, locked=False)
                    
                    # Set a new expiration time and update the database record
                    new_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
                    # This single call correctly updates the existing record or inserts if needed.
                    await self.db.create_hub_record(thread.id, channel.id, interaction.guild_id, language, interaction.user.id, new_expires_at)

                    # Send a confirmation message in the hub
                    reactivation_msg = f"This hub has been reactivated by {interaction.user.mention} and will now expire at {discord.utils.format_dt(new_expires_at, style='F')}."
                    await self._send_localized_hub_message(thread, language, reactivation_msg)

                    # Send a private confirmation to the user who initiated it
                    await interaction.followup.send(f"Successfully reactivated the existing hub: {thread.mention}", ephemeral=True)
                    return # IMPORTANT: Stop execution to prevent creating a new hub
            except discord.NotFound:
                log.warning(f"Found record for archived hub {archived_hub_record['thread_id']} but couldn't fetch it. Deleting record.")
                await self.db.delete_hub(archived_hub_record['thread_id'])
            except Exception as e:
                log.error(f"Error during hub reactivation for {archived_hub_record['thread_id']}: {e}")
                await interaction.followup.send("An error occurred while trying to reactivate the existing hub.", ephemeral=True)
                return
        
        translated_channel_name = await self.translator.translate_text(channel.name.replace('-', ' '), language)
        if translated_channel_name:
            await self.usage.record_usage(len(channel.name))
        else:
            translated_channel_name = channel.name

        country_code = LANG_TO_COUNTRY_CODE.get(language)
        # Generate the flag emoji using our new helper function.
        flag = country_code_to_flag(country_code)
        
        hub_name = f"{flag} | {translated_channel_name}"
        
        try:
            thread = await channel.create_thread(
                name=hub_name,
                type=discord.ChannelType.private_thread,
                invitable=True # Correct parameter name, takes a boolean
            )
            log.info(f"Created new PRIVATE hub thread: '{hub_name}' ({thread.id})")
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to create **private** threads in that channel. Please check my permissions (needs 'Create Private Threads').", ephemeral=True)
            return

        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await self.db.create_hub_record(thread.id, channel.id, interaction.guild_id, language, interaction.user.id, expires_at)
        
        welcome_template = f"🌍 Welcome {interaction.user.mention} to the `{language}` translation hub for {channel.mention}!\n\nThis session expires at {discord.utils.format_dt(expires_at, style='F')}."
        await self._send_localized_hub_message(thread, language, welcome_template)
        
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Go to Hub", style=discord.ButtonStyle.link, url=thread.jump_url))
        
        if interaction.guild: # Only attempt to log to admin channel if in a guild context
            guild_id = interaction.guild.id
            guild_config = await self.db.get_guild_config(guild_id)
            admin_log_channel_id = None
            if guild_config:
                admin_log_channel_id = guild_config['admin_log_channel_id']

            if admin_log_channel_id:
                log_channel = self.bot.get_channel(admin_log_channel_id)
                if log_channel and isinstance(log_channel, discord.TextChannel):
                    await log_channel.send(f"➕ New hub created by {interaction.user.mention} for `{language}` in {channel.mention}. New hub: {thread.mention}")
                else:
                    log.warning(f"Configured admin log channel {admin_log_channel_id} for guild {guild_id} is invalid or not a text channel.")
            else:
                log.info(f"No admin log channel configured for guild {guild_id}.")
        else:
            log.warning("Cannot log new hub creation: Interaction not in a guild context.")
                
        await interaction.followup.send(f"Successfully created a new translation hub: {thread.mention}", view=view)

    @app_commands.command(name="translate_channel", description="Creates a live, two-way translation hub for this channel.")
    @app_commands.autocomplete(language=language_autocomplete)
    @app_commands.describe(language="The language for the new hub (e.g., es, de, ja).")
    async def create_hub_slash(self, interaction: discord.Interaction, language: str):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This command can only be run in a standard text channel.", ephemeral=True)
            return
        await self.create_hub_logic(interaction, language, interaction.channel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Check to ignore messages from the bot itself.
        if message.author == self.bot.user:
            return

        # This remains to ignore the bot's own webhook-sent translations.
        if message.webhook_id:
            return
        
        # This remains to ignore empty messages or DMs.
        if not (message.content or message.attachments) or not message.guild:
            return
        
        # --- HUB -> MAIN/OTHER HUBS ---
        if isinstance(message.channel, discord.Thread):
            origin_hub_record = await self.db.get_hub_by_thread_id(message.channel.id)
            if origin_hub_record and not origin_hub_record['is_archived']:
                # Get the list of ALL hubs for this source channel, including the origin one
                all_hubs = await self.db.get_hubs_by_source_channel(origin_hub_record['source_channel_id'])
                await self.handle_message_from_hub(message, origin_hub_record, all_hubs)
                return

        # --- MAIN -> HUBS ---
        if isinstance(message.channel, discord.TextChannel):
            active_hubs = await self.db.get_hubs_by_source_channel(message.channel.id)
            if active_hubs:
                await self.handle_message_from_source(message, active_hubs)


    # Handle_message_from_source (formerly relay_to_hubs)
    async def handle_message_from_source(self, message: discord.Message, hubs: List[asyncpg.Record]):
        """
        Translates a message from a source channel into all associated hub threads.
        This is the corrected version that ensures all hubs are processed.
        """
        log.info(f"Relaying message from source channel {message.channel.id} to {len(hubs)} hubs.")

        # --- Prepare message components once ---
        text_to_translate = message.content.strip() if message.content else ""
        attachment_links_str = "\n".join([att.proxy_url for att in message.attachments])
        current_source_flag_emoji = MAIN_LANGUAGE_FLAG
        current_guild_main_lang = MAIN_LANGUAGE

        if message.guild:
            guild_config = await self.db.get_guild_config(message.guild.id)
            if guild_config and guild_config['main_language_code']:
                current_guild_main_lang = guild_config['main_language_code']
                source_country_code = LANG_TO_COUNTRY_CODE.get(current_guild_main_lang, 'XX')
                current_source_flag_emoji = country_code_to_flag(source_country_code)

        # --- Loop through each hub and send the translated message ---
        for hub_record in hubs:
            target_lang = hub_record['language_code']
            thread_id = hub_record['thread_id']
            thread = self.bot.get_channel(thread_id)

            if not thread or not isinstance(thread, discord.Thread):
                log.warning(f"Hub thread {thread_id} not found for source channel {message.channel.id}. Skipping.")
                continue

            # Ensure we don't try to translate to the same language
            if current_guild_main_lang == target_lang:
                log.info(f"Skipping relay to hub {thread_id} because its language '{target_lang}' is the same as the source language.")
                continue

            translated_text = ""
            if text_to_translate:
                if self.usage.check_limit_exceeded(len(text_to_translate)):
                    log.warning(f"Translation to hub {thread.id} skipped: API limit reached.")
                    translated_text = f"[Translation Skipped] {text_to_translate}"
                else:
                    # The translator will handle remapping 'zh-TW' to 'zh'
                    translation_result = await self.translator.translate_text(text_to_translate, target_lang, source_language=current_guild_main_lang)
                    if translation_result:
                        await self.usage.record_usage(len(text_to_translate))
                        translated_text = translation_result
                    else:
                        log.error(f"Translation failed for '{text_to_translate}' to hub {thread.id} (lang: {target_lang}).")
                        # We continue here so we don't send a "Translation Failed" message
                        continue
            
            # Construct the final message content
            final_content_parts = []
            if translated_text:
                final_content_parts.append(translated_text)
            if attachment_links_str:
                final_content_parts.append(attachment_links_str)

            if not final_content_parts:
                log.info(f"No content (text or attachments) to forward from source channel {message.channel.id} to hub {thread.id}.")
                continue

            final_content = f"{current_source_flag_emoji} " + "\n".join(final_content_parts)
            
            await self._send_webhook_message(thread, final_content, message.author)


    # Handle_message_from_hub
        async def handle_message_from_hub(self, message: discord.Message, origin_hub_data: asyncpg.Record, all_hubs: List[asyncpg.Record]):
        """
        Translates a message from a hub thread back to the main source channel and all other associated hubs.
        """
        source_channel_id = origin_hub_data['source_channel_id']
        origin_lang_code = origin_hub_data['language_code']
        source_channel = self.bot.get_channel(source_channel_id)

        if not source_channel or not isinstance(source_channel, discord.TextChannel):
            log.warning(f"Source channel {source_channel_id} not found for hub {message.channel.id}. Skipping relay.")
            return

        # Prepare message components once
        origin_country_code = LANG_TO_COUNTRY_CODE.get(origin_lang_code, 'XX')
        origin_flag_emoji = country_code_to_flag(origin_country_code)
        text_to_translate = message.content.strip() if message.content else ""
        attachment_links_str = "\n".join([att.proxy_url for att in message.attachments])

        # Get main language for the guild
        current_guild_main_lang = MAIN_LANGUAGE
        if message.guild:
            guild_config = await self.db.get_guild_config(message.guild.id)
            if guild_config and guild_config.get('main_language_code'):
                current_guild_main_lang = guild_config['main_language_code']
        
        # --- Translate once for each target language needed ---
        translations = {}
        if text_to_translate:
            # Create a set of unique target languages to avoid redundant API calls
            target_langs = {hub['language_code'] for hub in all_hubs}
            target_langs.add(current_guild_main_lang)
            
            for lang in target_langs:
                if lang != origin_lang_code: # No need to translate to its own language
                    translations[lang] = await self.translator.translate_text(text_to_translate, lang, source_language=origin_lang_code)

            successful_translations = sum(1 for t in translations.values() if t is not None)
            if successful_translations > 0:
                await self.usage.record_usage(len(text_to_translate) * successful_translations)

        # --- 1. Send to Main Source Channel ---
        log.info(f"Relaying message from hub {message.channel.id} to source channel {source_channel_id} (target: {current_guild_main_lang})")
        main_text = translations.get(current_guild_main_lang)
        main_content = self.build_final_message(origin_flag_emoji, main_text, attachment_links_str, f"[Translation Failed] {text_to_translate}" if text_to_translate else "")
        if main_content:
            await self._send_webhook_message(source_channel, main_content, message.author)

        # --- 2. Send to ALL OTHER Hubs ---
        for other_hub_record in all_hubs:
            # Skip sending the message back to the hub it came from
            if other_hub_record['thread_id'] == message.channel.id:
                continue

            other_thread = self.bot.get_channel(other_hub_record['thread_id'])
            if not other_thread or not isinstance(other_thread, discord.Thread): continue

            target_lang_code = other_hub_record['language_code']
            log.info(f"Relaying message from hub {message.channel.id} to other hub {other_thread.id} (target: {target_lang_code})")
            
            other_text = translations.get(target_lang_code)
            other_content = self.build_final_message(origin_flag_emoji, other_text, attachment_links_str, f"[Translation Failed] {text_to_translate}" if text_to_translate else "")
            
            if other_content:
                await self._send_webhook_message(other_thread, other_content, message.author)

    # --- FIX: Place the build_final_message function here, at the correct class level ---
    def build_final_message(self, flag: str, translated_text: Optional[str], attachments: str, fallback_text: str) -> str:
        """Helper to construct the final message string."""
        content_parts = []
        
        # Use the translated text if available, otherwise use the fallback
        text_to_show = translated_text if translated_text is not None else fallback_text
        
        if text_to_show:
            content_parts.append(text_to_show)
        if attachments:
            content_parts.append(attachments)
            
        if not content_parts:
            return ""
            
        return f"{flag} " + "\n".join(content_parts)

    async def translate_channel_callback(self, interaction: discord.Interaction, message: discord.Message):
        """The actual logic for the 'Translate this Channel' context menu."""
        channel = message.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This action can only be used on a standard text channel.", ephemeral=True)
            return

        user_locale = await self.db.get_user_preferences(interaction.user.id)
        if not user_locale:
            await interaction.response.send_message("I don't know your preferred language. Please use the onboarding process or /set_language to set it.", ephemeral=True)
            return
        
        target_language = user_locale if user_locale in SUPPORTED_LANGUAGES else user_locale.split('-')[0]
        
        await self.create_hub_logic(interaction, target_language, channel)


# The setup function is now very simple
async def setup(bot: commands.Bot):
    """The setup function is now simple and clean."""
    if not all(hasattr(bot, attr) for attr in ['db_manager', 'translator', 'usage_manager']):
        log.critical("HubManagerCog cannot be loaded: Core services not found on bot object.")
        return

    await bot.add_cog(HubManagerCog(bot, bot.db_manager, bot.translator, bot.usage_manager))
    log.info("HUB_MANAGER_COG: Cog loaded, context menu registered in __init__.")