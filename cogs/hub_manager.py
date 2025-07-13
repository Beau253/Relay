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

# ... (all your top-level helper functions and classes like UITranslator and HubExtensionView are fine) ...
MAIN_LANGUAGE = 'en'
LANG_TO_COUNTRY_CODE = {
    'en': 'GB', 'es': 'ES', 'fr': 'FR', 'de': 'DE', 'it': 'IT', 'pt': 'PT',
    'ru': 'RU', 'zh': 'CN', 'zh-TW': 'TW', 'yue': 'HK', 'ja': 'JP',
    'ko': 'KR', 'ar': 'SA', 'hi': 'IN', 'id': 'ID', 'ms': 'MY',
    'vi': 'VN', 'ur': 'PK', 'nl': 'NL', 'sv': 'SE', 'no': 'NO',
    'da': 'DK', 'fi': 'FI', 'pl': 'PL', 'tr': 'TR'
}
def country_code_to_flag(code: str) -> str:
    OFFSET = 0x1F1E6 - ord('A')
    if not code or len(code) != 2: return '🏳️'
    code = code.upper()
    return chr(ord(code[0]) + OFFSET) + chr(ord(code[1]) + OFFSET)
MAIN_LANGUAGE_COUNTRY_CODE = LANG_TO_COUNTRY_CODE.get(MAIN_LANGUAGE, 'US')
MAIN_LANGUAGE_FLAG = country_code_to_flag(MAIN_LANGUAGE_COUNTRY_CODE)
class UITranslator:
    # ... (no changes needed here) ...
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
        translated = self.translations.get(locale, {}).get(key)
        if translated: return translated.format(**kwargs)
        base_lang = locale.split('-')[0]
        translated = self.translations.get(base_lang, {}).get(key)
        if translated: return translated.format(**kwargs)
        translated = self.translations.get('en', {}).get(key, key)
        return translated.format(**kwargs)
ui_translator = UITranslator()
class HubExtensionView(discord.ui.View):
    # ... (no changes needed here) ...
    def __init__(self, target_lang: str):
        super().__init__(timeout=None)
        self.target_lang = target_lang
        self.selected_duration: Optional[int] = None
    @classmethod
    async def create(cls, db: DatabaseManager, target_lang: str):
        view = cls(target_lang)
        select_placeholder = ui_translator.get_string("HubUI-ExtendPlaceholder", view.target_lang)
        extend_button_label = ui_translator.get_string("HubUI-ExtendButton", view.target_lang)
        options = [
            discord.SelectOption(label=ui_translator.get_string("HubUI-Duration5m", view.target_lang), value="5"),
            discord.SelectOption(label=ui_translator.get_string("HubUI-Duration15m", view.target_lang), value="15"),
            discord.SelectOption(label=ui_translator.get_string("HubUI-Duration30m", view.target_lang), value="30"),
            discord.SelectOption(label=ui_translator.get_string("HubUI-Duration1h", view.target_lang), value="60")]
        select_menu = discord.ui.Select(custom_id="hub:duration_select", placeholder=select_placeholder, options=options)
        extend_button = discord.ui.Button(label=extend_button_label, style=discord.ButtonStyle.success, custom_id="hub:extend_button")
        select_menu.callback = view.duration_select_callback
        extend_button.callback = view.extend_button_callback
        view.db = db
        view.add_item(select_menu)
        view.add_item(extend_button)
        return view
    async def duration_select_callback(self, interaction: discord.Interaction):
        self.selected_duration = int(interaction.data['values'][0])
        await interaction.response.defer()
    async def extend_button_callback(self, interaction: discord.Interaction):
        if self.selected_duration is None:
            error_msg = ui_translator.get_string("HubUI-ErrorSelectFirst", self.target_lang)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        minutes_to_add = self.selected_duration
        new_expiry_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_add)
        updated = await self.db.update_hub_expiry(interaction.channel.id, new_expiry_time)
        if updated:
            log.info(f"Hub {interaction.channel.id} extended by {minutes_to_add} minutes by user {interaction.user.id}")
            expiry_formatted = discord.utils.format_dt(new_expiry_time, style='F')
            confirmation_msg = ui_translator.get_string("HubUI-ConfirmExtended", self.target_lang, expiry_time=expiry_formatted)
            await interaction.response.send_message(confirmation_msg)
            await interaction.message.delete()
        else:
            error_msg = ui_translator.get_string("HubUI-ErrorExpired", self.target_lang)
            await interaction.response.send_message(error_msg, ephemeral=True)

@app_commands.guild_only()
class HubManagerCog(commands.Cog, name="Hub Manager"):
    def __init__(self, bot: commands.Bot, db: DatabaseManager, translator: TextTranslator, usage: UsageManager):
        self.bot = bot
        self.db = db
        self.translator = translator
        self.usage = usage
        self.webhook_cache: Dict[int, discord.Webhook] = {}

        # --- THIS IS THE CORRECTED PART ---
        # 1. Manually create the ContextMenu object
        hub_menu = app_commands.ContextMenu(
            name='Translate this Channel',
            callback=self.translate_channel_callback, # Point to the callback method below
        )
        # 2. Add it to the cog's application commands
        self.add_app_command(hub_menu)
        # --- END CORRECTION ---

        self.check_hubs_for_warnings.start()
        self.check_hubs_for_expiration.start()

    # ... (all your other methods like cog_unload, helpers, tasks, create_hub_logic, etc., are fine) ...
    def cog_unload(self):
        self.check_hubs_for_warnings.cancel()
        self.check_hubs_for_expiration.cancel()
    async def _send_localized_hub_message(self, thread: discord.Thread, target_lang: str, english_text: str, view: Optional[discord.ui.View] = None):
        translated_text = await self.translator.translate_text(english_text, target_lang)
        if translated_text: await self.usage.record_usage(len(english_text))
        else: translated_text = english_text
        await thread.send(translated_text, view=view)
    async def _get_webhook(self, channel: discord.TextChannel | discord.Thread) -> Optional[discord.Webhook]:
        target_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        if target_channel.id in self.webhook_cache: return self.webhook_cache[target_channel.id]
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
            if isinstance(channel, discord.Thread): await webhook.send(content=content, username=username_to_use, avatar_url=author.display_avatar.url, thread=channel)
            else: await webhook.send(content=content, username=username_to_use, avatar_url=author.display_avatar.url)
        except (discord.Forbidden, discord.NotFound) as e:
            log.error(f"Failed to send webhook message to {channel.id}: {e}")
    @tasks.loop(minutes=1)
    async def check_hubs_for_warnings(self):
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
        await self.bot.wait_until_ready()
        log.info("HubManagerCog: 'check_hubs_for_warnings' loop is ready.")
    @tasks.loop(minutes=1)
    async def check_hubs_for_expiration(self):
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
        if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
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
                thread = await self.bot.fetch_channel(archived_hub_record['thread_id'])
                if isinstance(thread, discord.Thread):
                    log.info(f"Reactivating archived hub {thread.id} for user {interaction.user.id}")
                    await thread.edit(archived=False, locked=False)
                    new_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
                    await self.db.create_hub_record(thread.id, channel.id, interaction.guild_id, language, interaction.user.id, new_expires_at)
                    reactivation_msg = f"This hub has been reactivated by {interaction.user.mention} and will now expire at {discord.utils.format_dt(new_expires_at, style='F')}."
                    await self._send_localized_hub_message(thread, language, reactivation_msg)
                    await interaction.followup.send(f"Successfully reactivated the existing hub: {thread.mention}", ephemeral=True)
                    return
            except discord.NotFound:
                log.warning(f"Found record for archived hub {archived_hub_record['thread_id']} but couldn't fetch it. Deleting record.")
                await self.db.delete_hub(archived_hub_record['thread_id'])
            except Exception as e:
                log.error(f"Error during hub reactivation for {archived_hub_record['thread_id']}: {e}")
                await interaction.followup.send("An error occurred while trying to reactivate the existing hub.", ephemeral=True)
                return
        translated_channel_name = await self.translator.translate_text(channel.name.replace('-', ' '), language)
        if translated_channel_name: await self.usage.record_usage(len(channel.name))
        else: translated_channel_name = channel.name
        country_code = LANG_TO_COUNTRY_CODE.get(language)
        flag = country_code_to_flag(country_code)
        hub_name = f"{flag} | {translated_channel_name}"
        try:
            thread = await channel.create_thread(name=hub_name, type=discord.ChannelType.private_thread, invitable=True)
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
        if interaction.guild:
            guild_id = interaction.guild.id
            guild_config = await self.db.get_guild_config(guild_id)
            admin_log_channel_id = None
            if guild_config: admin_log_channel_id = guild_config['admin_log_channel_id']
            if admin_log_channel_id:
                log_channel = self.bot.get_channel(admin_log_channel_id)
                if log_channel and isinstance(log_channel, discord.TextChannel):
                    await log_channel.send(f"➕ New hub created by {interaction.user.mention} for `{language}` in {channel.mention}. New hub: {thread.mention}")
                else: log.warning(f"Configured admin log channel {admin_log_channel_id} for guild {guild_id} is invalid or not a text channel.")
            else: log.info(f"No admin log channel configured for guild {guild_id}.")
        else: log.warning("Cannot log new hub creation: Interaction not in a guild context.")
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
        if message.author.bot and message.author.id == self.bot.user.id: return
        if message.webhook_id: return
        if not (message.content or message.attachments) or not message.guild: return
        if isinstance(message.channel, discord.Thread):
            hub_record = await self.db.get_hub_by_thread_id(message.channel.id)
            if hub_record:
                await self.handle_message_from_hub(message, hub_record)
                return
        if isinstance(message.channel, discord.TextChannel):
            source_hubs = await self.db.get_hubs_by_source_channel(message.channel.id)
            if source_hubs:
                await self.handle_message_from_source(message, source_hubs)
    async def handle_message_from_source(self, message: discord.Message, hubs: List[asyncpg.Record]):
        log.info(f"Relaying message from source channel {message.channel.id} to {len(hubs)} hubs.")
        current_guild_main_lang = MAIN_LANGUAGE
        current_source_flag_emoji = MAIN_LANGUAGE_FLAG
        if message.guild:
            guild_config = await self.db.get_guild_config(message.guild.id)
            if guild_config and guild_config['main_language_code']:
                current_guild_main_lang = guild_config['main_language_code']
                source_country_code = LANG_TO_COUNTRY_CODE.get(current_guild_main_lang)
                current_source_flag_emoji = country_code_to_flag(source_country_code)
        attachment_links_str = ""
        if message.attachments: attachment_links_str = "\n".join([att.proxy_url for att in message.attachments])
        for hub_record in hubs:
            target_lang = hub_record['language_code']
            thread_id = hub_record['thread_id']
            thread = self.bot.get_channel(thread_id)
            if not thread or not isinstance(thread, discord.Thread):
                log.warning(f"Hub thread {thread_id} not found for source channel {message.channel.id}. Skipping relay to this hub.")
                continue
            text_to_translate = message.content.strip() if message.content else ""
            translated_text = ""
            if text_to_translate:
                if self.usage.check_limit_exceeded(len(text_to_translate)): 
                    log.warning(f"Translation to hub {thread_id} skipped: API usage limit has been reached.")
                    translated_text = f"[Translation Skipped] {text_to_translate}"
                else:
                    translation_result = await self.translator.translate_text(text_to_translate, target_lang, source_language=current_guild_main_lang)
                    if translation_result:
                        await self.usage.record_usage(len(text_to_translate))
                        translated_text = translation_result
                    else:
                        log.error(f"Translation failed for '{text_to_translate}' to hub {thread_id}.")
                        translated_text = f"[Translation Failed] {text_to_translate}"
            final_content_parts = []
            if translated_text: final_content_parts.append(f"{current_source_flag_emoji} {translated_text}")
            if attachment_links_str:
                if not translated_text: final_content_parts.append(f"{current_source_flag_emoji}")
                final_content_parts.append(attachment_links_str)
            final_content = "\n".join(final_content_parts)
            if final_content: await self._send_webhook_message(thread, final_content, message.author)
            else: log.info(f"No content to forward from source channel {message.channel.id} to hub {thread_id}.")
    async def handle_message_from_hub(self, message: discord.Message, hub_data: asyncpg.Record):
        source_channel_id = hub_data['source_channel_id']
        origin_lang_code = hub_data['language_code']
        source_channel = self.bot.get_channel(source_channel_id)
        if not source_channel or not isinstance(source_channel, discord.TextChannel):
            log.warning(f"Source channel {source_channel_id} not found for hub {message.channel.id}. Skipping relay.")
            return
        origin_country_code = LANG_TO_COUNTRY_CODE.get(origin_lang_code)
        origin_flag_emoji = country_code_to_flag(origin_country_code)
        current_guild_main_lang = MAIN_LANGUAGE
        if message.guild:
            guild_config = await self.db.get_guild_config(message.guild.id)
            if guild_config and guild_config['main_language_code']:
                current_guild_main_lang = guild_config['main_language_code']
        attachment_links_str = ""
        if message.attachments: attachment_links_str = "\n".join([att.proxy_url for att in message.attachments])
        text_to_translate = message.content.strip() if message.content else ""
        log.info(f"Relaying message from hub {message.channel.id} to source channel {source_channel_id} (target: {current_guild_main_lang})")
        to_main_text = ""
        if text_to_translate:
            if not self.usage.check_limit_exceeded(len(text_to_translate)):
                translation_result = await self.translator.translate_text(text_to_translate, current_guild_main_lang, source_language=origin_lang_code)
                if translation_result:
                    await self.usage.record_usage(len(text_to_translate))
                    to_main_text = translation_result
                else:
                    log.error(f"Translation to main channel failed for '{text_to_translate}'.")
                    to_main_text = f"[Translation Failed] {text_to_translate}"
            else:
                log.warning(f"Translation to main channel skipped from hub {message.channel.id}: API usage limit reached.")
                to_main_text = f"[Translation Skipped] {text_to_translate}"
        final_content_to_main_parts = []
        if to_main_text: final_content_to_main_parts.append(f"{origin_flag_emoji} {to_main_text}")
        if attachment_links_str:
            if not to_main_text: final_content_to_main_parts.append(f"{origin_flag_emoji}")
            final_content_to_main_parts.append(attachment_links_str)
        final_content_to_main = "\n".join(final_content_to_main_parts)
        if final_content_to_main: await self._send_webhook_message(source_channel, final_content_to_main, message.author)
        else: log.info(f"No content to forward from hub {message.channel.id} to main channel.")
        all_hubs_for_source = await self.db.get_hubs_by_source_channel(source_channel_id)
        for other_hub_record in all_hubs_for_source:
            other_thread_id = other_hub_record['thread_id']
            if other_thread_id == message.channel.id: continue
            other_thread = self.bot.get_channel(other_thread_id)
            if not other_thread or not isinstance(other_thread, discord.Thread):
                log.warning(f"Other hub thread {other_thread_id} not found. Skipping relay from {message.channel.id}.")
                continue
            target_lang_code = other_hub_record['language_code']
            log.info(f"Relaying message from hub {message.channel.id} to other hub {other_thread_id} (target: {target_lang_code})")
            to_other_hub_text = None
            if text_to_translate:
                if not self.usage.check_limit_exceeded(len(text_to_translate)):
                    to_other_hub_text = await self.translator.translate_text(text_to_translate, target_lang_code, source_language=origin_lang_code)
                else:
                    log.warning(f"Translation to other hub {other_thread_id} skipped from hub {message.channel.id}: API usage limit of {self.usage.safe_limit} has been reached for text content.")
            final_content_to_other_hub = ""
            if to_other_hub_text:
                await self.usage.record_usage(len(text_to_translate))
                final_content_to_other_hub = f"{origin_flag_emoji} {to_other_hub_text}"
            elif text_to_translate:
                final_content_to_other_hub = f"{origin_flag_emoji} [Translation Failed/Skipped] {text_to_translate}"
                log.error(f"Failed/skipped translation for '{text_to_translate}' to other hub {other_thread_id}. Forwarding original text with attachments.")
            if attachment_links_str:
                if final_content_to_other_hub: final_content_to_other_hub += attachment_links_str
                else: final_content_to_other_hub = f"{origin_flag_emoji} {attachment_links_str.strip()}"

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

async def setup(bot: commands.Bot):
    """The setup function for the cog."""
    if not all(hasattr(bot, attr) for attr in ['db_manager', 'translator', 'usage_manager']):
        log.critical("HubManagerCog cannot be loaded: Core services not found on bot object.")
        return

    await bot.add_cog(HubManagerCog(bot, bot.db_manager, bot.translator, bot.usage_manager))
    log.info("HUB_MANAGER_COG: Cog loaded and context menu added to tree.")