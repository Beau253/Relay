import discord
import logging
import os
import json
import re
import random
from discord.ext import commands
from discord import app_commands
from cogs.hub_manager import HubManagerCog
from langdetect import detect, LangDetectException
from typing import Optional, List

# Import our core services and utilities
from core import DatabaseManager, TextTranslator, UsageManager, language_autocomplete, SUPPORTED_LANGUAGES
from core.utils import country_code_to_flag

log = logging.getLogger(__name__)

# This dictionary is ESSENTIAL for flags that cannot be generated from a simple two-letter code.
SPECIAL_CASE_FLAGS = {
    "GB-ENG": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "GB-SCT": "🏴󠁧󠁢󠁳󠁣󠁴󠁿",
    "GB-WLS": "🏴󠁧󠁢󠁷󠁬󠁳󠁿",
    "EU": "🇪🇺",
    "UN": "🇺🇳"
}

class GlossaryEntryModal(discord.ui.Modal, title='Add to Dictionary'):
    term_input = discord.ui.TextInput(
        label='Term to protect from translation',
        style=discord.TextStyle.short,
        placeholder='e.g., Shinny, a special project name, etc.',
        required=True,
        max_length=100
    )

    def __init__(self, db: DatabaseManager):
        super().__init__()
        self.db = db

    async def on_submit(self, interaction: discord.Interaction):
        term = self.term_input.value.strip()
        if not term:
            await interaction.response.send_message("The term cannot be empty.", ephemeral=True)
            return
        
        if not interaction.guild_id:
            return

        await self.db.add_glossary_term(interaction.guild_id, term)
        log.info(f"User {interaction.user.id} added term '{term}' to glossary for guild {interaction.guild_id}.")
        await interaction.response.send_message(f"✅ The term `{term}` has been added to the server's dictionary and will be protected from translation.", ephemeral=True)

@app_commands.guild_only()
class TranslationCog(commands.Cog, name="Translation"):
    def __init__(self, bot: commands.Bot, db_manager: DatabaseManager, translator: TextTranslator, usage_manager: UsageManager):
        self.bot = bot
        self.db = db_manager
        self.translator = translator
        self.usage = usage_manager
        self.emoji_to_language_map: dict[str, str] = {}
        self.pirate_dict: dict[str, str] = {}
        self.webhook_cache: dict[int, discord.Webhook] = {}
        self._load_flag_data()
        self._load_pirate_data()

        log.info("[TRANSLATION_COG] Initializing and adding context menus...")
        self.translate_message_menu = app_commands.ContextMenu(
            name='Translate Message',
            callback=self.translate_message_callback,
        )
        self.add_to_dictionary_menu = app_commands.ContextMenu(
            name='Add to Dictionary',
            callback=self.add_to_dictionary_callback,
        )
        self.bot.tree.add_command(self.translate_message_menu)
        self.bot.tree.add_command(self.add_to_dictionary_menu)
        log.info("[TRANSLATION_COG] Context menus added to tree.")

    def _load_flag_data(self):
        try:
            script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(script_dir, 'data', 'flags.json')
            with open(file_path, 'r', encoding='utf-8') as f:
                flag_data = json.load(f)

            for _, data in flag_data.items():
                country_code = data.get("countryCode")
                languages = data.get("languages")
                if country_code and languages:
                    emoji = SPECIAL_CASE_FLAGS.get(country_code) or country_code_to_flag(country_code)
                    if emoji and (emoji != '🏳️' or country_code in SPECIAL_CASE_FLAGS):
                         self.emoji_to_language_map[emoji] = languages[0]
            log.info(f"Successfully loaded {len(self.emoji_to_language_map)} emoji-to-language mappings.")
        except FileNotFoundError:
            log.error("Could not find data/flags.json. Flag reaction translations will not work.")
        except json.JSONDecodeError as e:
            log.critical(f"FATAL: flags.json has a syntax error: {e}. Flag reactions will not work.")
        except Exception as e:
            log.error(f"Error loading flags.json: {e}", exc_info=True)
            
    def _load_pirate_data(self):
        try:
            script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(script_dir, 'data', 'pirate_speak.json')
            with open(file_path, 'r', encoding='utf-8') as f:
                self.pirate_dict = json.load(f)
            log.info(f"Successfully loaded {len(self.pirate_dict)} pirate speak phrases.")
        except FileNotFoundError:
            log.warning("Could not find data/pirate_speak.json. Pirate translations will be disabled.")
        except json.JSONDecodeError as e:
            log.error(f"FATAL: pirate_speak.json has a syntax error: {e}. Pirate translations will be disabled.")
        except Exception as e:
            log.error(f"Error loading pirate_speak.json: {e}", exc_info=True)

    def _translate_to_pirate_speak(self, text: str) -> str:
        if not self.pirate_dict:
            return "Arr, me dictionary be lost at sea!"
        sorted_phrases = sorted(self.pirate_dict.keys(), key=len, reverse=True)
        for phrase in sorted_phrases:
            text = re.sub(r'\b' + re.escape(phrase) + r'\b', self.pirate_dict[phrase], text, flags=re.IGNORECASE)
        exclamations = ["Arrr!", "Shiver me timbers!", "Yo ho ho!", "Blimey!"]
        return f"{text} {random.choice(exclamations)}"

    def cog_unload(self):
        self.bot.tree.remove_command(self.translate_message_menu.name, type=self.translate_message_menu.type)

    async def perform_translation(self, original_message_content: str, target_lang: str):
        if not self.translator.is_initialized:
            return {"translated_text": "Translation service is currently unavailable.", "detected_language_code": "error"}
        if self.usage.check_limit_exceeded(len(original_message_content)):
            return {"translated_text": "The monthly translation limit has been reached.", "detected_language_code": "error"}
        
        translation_result = await self.translator.translate_text(original_message_content, target_lang)
        if translation_result and translation_result.get('translated_text') and translation_result.get("detected_language_code") != "error":
            await self.usage.record_usage(len(original_message_content))
        return translation_result

    async def translate_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        """The actual logic for the 'Translate Message' context menu."""
        await interaction.response.defer(ephemeral=True)
        if not message.content and not message.embeds:
            await interaction.followup.send("This message has no text or embeds to translate.")
            return
        target_language = await self.db.get_user_preferences(interaction.user.id)
        if not target_language:
            await interaction.followup.send("I don't know your preferred language yet! Use /set_language to set it up.", ephemeral=True)
            return
        
        # --- Glossary Integration ---
        glossary = await self.db.get_glossary_terms(interaction.guild_id) if interaction.guild_id else []
        
        translated_text = ""
        if message.content:
            translation_result = await self.perform_translation(message.content, target_language, glossary=glossary)
            if translation_result:
                translated_text = translation_result.get('translated_text', '')
        
        translated_embeds = []
        if message.embeds:
            for embed in message.embeds:
                translated_embed = await HubManagerCog._translate_embed(self.translator, embed, target_language, glossary=glossary)
                translated_embeds.append(translated_embed)
        
        if translated_text and not translated_embeds:
            reply_embed = discord.Embed(title="Translation Result", description=translated_text, color=discord.Color.blue())
            reply_embed.set_footer(text=f"Original message by {message.author.display_name}")
            await interaction.followup.send(embed=reply_embed)
        elif translated_embeds:
            if translated_text:
                await interaction.followup.send(translated_text, embeds=translated_embeds)
            else:
                await interaction.followup.send(embeds=translated_embeds)
        elif not translated_text and message.content:
             await interaction.followup.send("An error occurred during translation.", ephemeral=True)

    async def add_to_dictionary_callback(self, interaction: discord.Interaction, message: discord.Message):
        """The logic for the 'Add to Dictionary' context menu."""
        modal = GlossaryEntryModal(self.db)
        # Pre-fill the form with the content of the message the user clicked on
        if message.content:
            modal.term_input.default = message.content
        await interaction.response.send_modal(modal)

    async def _get_webhook(self, channel: discord.TextChannel) -> Optional[discord.Webhook]:
        if channel.id in self.webhook_cache:
            return self.webhook_cache[channel.id]
        try:
            webhooks = await channel.webhooks()
            webhook = discord.utils.get(webhooks, name="Relay Translator")
            if webhook is None:
                webhook = await channel.create_webhook(name="Relay Translator")
            self.webhook_cache[channel.id] = webhook
            return webhook
        except discord.Forbidden:
            log.error(f"Missing 'Manage Webhooks' permission in #{channel.name} for impersonation.")
            return None
        except Exception as e:
            log.error(f"Failed to get/create webhook for #{channel.name}: {e}", exc_info=True)
            return None
    
    async def _send_webhook_as_reply(self, message: discord.Message, content: str):
        webhook = await self._get_webhook(message.channel)
        if not webhook:
            await message.reply(content, mention_author=False) # Fallback to normal reply
            return
        try:
            # NOTE: Webhooks cannot create direct "replies". This will send a new message
            # into the channel, impersonating the user. The `delete_original` flag becomes
            # important for this workflow to feel clean.
            await webhook.send(
                content=content,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
                allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.NotFound):
            await message.reply(content, mention_author=False) # Fallback
    
    @app_commands.command(name="set_language", description="Set your preferred language for translations.")
    @app_commands.autocomplete(language=language_autocomplete)
    @app_commands.describe(language="The language you want messages to be translated into for you.")
    async def set_language(self, interaction: discord.Interaction, language: str):
        if language not in SUPPORTED_LANGUAGES:
            await interaction.response.send_message(f"Sorry, `{language}` is not a supported language code.", ephemeral=True)
            return
        try:
            await self.db.set_user_preferences(user_id=interaction.user.id, user_locale=language)
            await interaction.response.send_message(f"Your preferred language has been set to **{SUPPORTED_LANGUAGES[language]}** (`{language}`).", ephemeral=True)
        except Exception:
            await interaction.response.send_message("An error occurred while saving your preference.", ephemeral=True)

    def _is_likely_english_slang(self, text: str) -> bool:
        """
        A heuristic pre-filter to catch exaggerated English or short, common phrases
        before they hit the API. Returns True if the text is likely slang.
        """
        # Rule 1: Check for excessive character repetition (e.g., "heyyyyy", "soooooo")
        if re.search(r'(.)\1{3,}', text):
            return True

        # Rule 2: Check for very short, common English words that can be misidentified.
        # This prevents single-word replies like "ok", "lol", "ty" from being translated.
        lower_text = text.lower()
        if lower_text in ["ok", "lol", "ty", "thanks", "omg", "heh", "okey", "thx", "np"]:
            return True

        # Rule 3: Check for messages that are just a single, non-translatable word.
        if len(text.split()) == 1 and not re.search(r'\s', text):
            # A simple check: if it's short and has no vowels, it's likely an acronym or slang.
            if len(text) < 6 and not any(char in 'aeiouAEIOU' for char in text):
                return True

        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id or not message.guild or not isinstance(message.channel, discord.TextChannel) or not message.content:
            return
            
        # --- NEW HEURISTIC PRE-FILTER ---
        if self._is_likely_english_slang(message.content):
            log.info(f"Auto-translate skipped: Heuristic pre-filter identified message '{message.content}' as likely slang. No API call made.")
            return
        # --- END OF HEURISTIC PRE-FILTER ---

        # --- Translation Rule Hierarchy ---
        # 1. Check for a channel-specific rule
        config = await self.db.get_auto_translate_config(message.channel.id)

        # 2. If no channel rule, check for a server-wide rule
        if not config:
            # 2a. First, check if the channel is explicitly exempt
            if await self.db.is_channel_exempt(message.channel.id):
                return
            
            # 2b. If not exempt, get the server config to find the server-wide language
            guild_config = await self.db.get_guild_config(message.guild.id)
            server_lang = guild_config.get('server_wide_language') if guild_config else None
            
            if server_lang:
                # Create a "virtual" config for the server-wide rule using the stored settings
                config = {
                    'target_language_code': server_lang,
                    'impersonate': guild_config.get('sw_impersonate', False),
                    'delete_original': guild_config.get('sw_delete_original', False)
                }
            else:
                # No channel rule and no server rule, so we are done.
                return
        
        target_lang = config['target_language_code']

        # --- Pre-filter to save API calls ---
        try:
            detected_lang = detect(message.content)
            if detected_lang.split('-')[0] == target_lang.split('-')[0]:
                return
        except LangDetectException:
            pass

        # --- Glossary Integration ---
        glossary = await self.db.get_glossary_terms(message.guild.id)
        
        translation_result = await self.perform_translation(message.content, target_lang, glossary=glossary)
        if not translation_result: return

        translated_text = translation_result.get('translated_text')
        detected_language = translation_result.get('detected_language_code')
        
        # Final check: Don't post if translation failed or resulted in the same text
        if not translated_text or not detected_language or detected_language == "error" or translated_text == message.content:
            return
        
        # --- Post Translation and Delete Original if configured ---
        if config.get('impersonate', False):
            await self._send_webhook_as_reply(message, translated_text)
        else:
            await message.reply(content=translated_text, mention_author=False)

        if config.get('delete_original', False):
            try:
                await message.delete()
            except discord.Forbidden:
                log.warning(f"Failed to delete original message {message.id} in #{message.channel.name}: Missing 'Manage Messages' permission.")
            except discord.NotFound:
                pass
            except Exception as e:
                log.error(f"An unexpected error occurred while deleting message {message.id}: {e}", exc_info=True)
                
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or (payload.member and payload.member.bot):
            return
            
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)): return
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        # --- PIRATE SPEAK FEATURE ---
        if str(payload.emoji) == '🏴‍☠️':
            if message.content:
                log.info(f"Pirate speak triggered by {payload.member.display_name if payload.member else 'Unknown User'}.")
                pirate_text = self._translate_to_pirate_speak(message.content)
                await message.reply(content=pirate_text, mention_author=False)
            return
            
        target_language = self.emoji_to_language_map.get(str(payload.emoji))
        if not target_language:
            return
            
        if not message.content and not message.embeds:
            return

        # --- OFFLINE PRE-FILTER RESTORED ---
        if message.content:
            try:
                detected_lang = detect(message.content)
                if detected_lang.split('-')[0] == target_language.split('-')[0]:
                    log.info(f"Flag reaction skipped: Local pre-filter detected '{detected_lang}', matching target '{target_language}'. No API call.")
                    return
            except LangDetectException:
                log.warning("Local detection failed for flag reaction, falling back to Google API.")
                pass
        # --- END OF PRE-FILTER ---

        log.info(f"Flag reaction translation triggered by {payload.member.display_name if payload.member else 'Unknown User'} for language '{target_language}'.")
        async with channel.typing():
            translated_text = ""
            if message.content:
                translation_result = await self.perform_translation(message.content, target_language)
                if translation_result:
                    translated_text = translation_result.get('translated_text', '')
            translated_embeds = []
            if message.embeds:
                for embed in message.embeds:
                    translated_embed = await HubManagerCog._translate_embed(self.translator, embed, target_language)
                    translated_embeds.append(translated_embed)
            if translated_text or translated_embeds:
                await message.reply(content=translated_text, embeds=translated_embeds, mention_author=False)

    async def translate_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer(ephemeral=True)
        if not message.content and not message.embeds:
            await interaction.followup.send("This message has no text or embeds to translate.")
            return
        target_language = await self.db.get_user_preferences(interaction.user.id)
        if not target_language:
            await interaction.followup.send("I don't know your preferred language yet! Please use /set_language to set it up.", ephemeral=True)
            return
        translated_text = ""
        if message.content:
            translation_result = await self.perform_translation(message.content, target_language)
            if translation_result:
                translated_text = translation_result.get('translated_text', '')
        translated_embeds = []
        if message.embeds:
            for embed in message.embeds:
                translated_embed = await HubManagerCog._translate_embed(self.translator, embed, target_language)
                translated_embeds.append(translated_embed)
        if translated_text and not translated_embeds:
            reply_embed = discord.Embed(title="Translation Result", description=translated_text, color=discord.Color.blue())
            reply_embed.set_footer(text=f"Original message by {message.author.display_name}")
            await interaction.followup.send(embed=reply_embed)
        elif translated_embeds:
            if translated_text:
                await interaction.followup.send(translated_text, embeds=translated_embeds)
            else:
                await interaction.followup.send(embeds=translated_embeds)
        elif not translated_text and message.content:
             await interaction.followup.send("An error occurred during translation.", ephemeral=True)

async def setup(bot: commands.Bot):
    if not all(hasattr(bot, attr) for attr in ['db_manager', 'translator', 'usage_manager']):
        log.critical("TranslationCog cannot be loaded: Core services not found on bot object.")
        return
    await bot.add_cog(TranslationCog(bot, bot.db_manager, bot.translator, bot.usage_manager))
    log.info("TRANSLATION_COG: Cog loaded, context menu registered in __init__.")