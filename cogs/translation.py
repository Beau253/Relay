import discord
import logging
import os
import json
import re
import random
from discord.ext import commands
from discord import app_commands
from cogs.hub_manager import HubManagerCog
from lingua import LanguageDetectorBuilder, Language
from typing import Optional, List
from thefuzz import process, fuzz # For fuzzy string matching

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

    def __init__(self, db: DatabaseManager, thread_to_delete: Optional[discord.Thread] = None):
        super().__init__()
        self.db = db
        self.thread_to_delete = thread_to_delete

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        term = self.term_input.value.strip()
        if not term:
            await interaction.followup.send("The term cannot be empty.", ephemeral=True)
            return
        
        if not interaction.guild_id:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        await self.db.add_glossary_term(interaction.guild_id, term)
        log.info(f"User {interaction.user.id} added term '{term}' to glossary for guild {interaction.guild_id}.")
        await interaction.followup.send(f"✅ The term `{term}` has been added to the server's dictionary.", ephemeral=True)

        # If a thread was passed to the modal, it means it came from the correction UI.
        # Delete the thread after the action is complete.
        if self.thread_to_delete:
            try:
                log.info(f"Deleting correction thread {self.thread_to_delete.id} after dictionary add.")
                await self.thread_to_delete.delete()
            except discord.HTTPException as e:
                log.error(f"Failed to delete correction thread {self.thread_to_delete.id}: {e}")

class CorrectionView(discord.ui.View):
    """A view with buttons to handle a potential auto-correction within a private thread."""
    message: discord.Message # To help type-hinting

    def __init__(self, cog: "TranslationCog", original_message: discord.Message, suggested_term: str):
        # Timeout is 5 minutes
        super().__init__(timeout=300) 
        self.cog = cog
        self.original_message = original_message
        self.suggested_term = suggested_term
        self.original_author_id = original_message.author.id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensures only the original message author can use the buttons."""
        if interaction.user.id != self.original_author_id:
            await interaction.response.send_message("You are not the author of this message.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        """When the view times out, delete the private thread."""
        # The view's message is the bot's interactive prompt. Its channel is the thread.
        if self.message and isinstance(self.message.channel, discord.Thread):
            try:
                log.info(f"Correction thread {self.message.channel.id} timed out. Deleting.")
                await self.message.channel.delete()
            except discord.NotFound:
                pass # Thread already deleted, which is fine.

    @discord.ui.button(label="Ignore", style=discord.ButtonStyle.secondary)
    async def ignore_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Deletes the private thread. The original message remains."""
        await interaction.response.defer()
        if isinstance(interaction.channel, discord.Thread):
            try:
                log.info(f"User ignored correction. Deleting thread {interaction.channel.id}.")
                await interaction.channel.delete()
            except discord.HTTPException as e:
                log.error(f"Failed to delete thread {interaction.channel.id} on 'Ignore': {e}")

    @discord.ui.button(label="Send", style=discord.ButtonStyle.success)
    async def send_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Sends the corrected message, deletes the original, and deletes the thread."""
        await interaction.response.defer()
        try:
            await self.cog._send_corrected_message(self.original_message, self.suggested_term)
            await self.original_message.delete()
            if isinstance(interaction.channel, discord.Thread):
                await interaction.channel.delete()
        except discord.Forbidden:
            log.error(f"Missing permissions to manage messages/webhooks for correction 'Send' action.")
            if interaction.channel:
                 await interaction.followup.send("I lack permissions to send the message or delete the original.", ephemeral=False)
        except Exception as e:
            log.error(f"Error during correction 'Send' action: {e}", exc_info=True)
    
    @discord.ui.button(label="Add to Dictionary", style=discord.ButtonStyle.primary)
    async def add_to_dictionary_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Presents a modal to add the user's *original* term to the dictionary."""
        thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        
        modal = GlossaryEntryModal(self.cog.db, thread_to_delete=thread)
        modal.term_input.default = self.original_message.content.strip()
        await interaction.response.send_modal(modal)


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
        # Use the more reliable 'lingua' library for language detection
        self.detector = LanguageDetectorBuilder.from_languages(
            *[Language[lang.upper().replace("-", "_")] for lang in SUPPORTED_LANGUAGES]
        ).with_preloaded_language_models().build()
        
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
        self.report_translation_menu = app_commands.ContextMenu(
            name='Report Translation',
            callback=self.report_translation_callback,
        )
        self.bot.tree.add_command(self.translate_message_menu)
        self.bot.tree.add_command(self.add_to_dictionary_menu)
        self.bot.tree.add_command(self.report_translation_menu)
        log.info("[TRANSLATION_COG] Context menus added to tree.")

    def _load_flag_data(self):
        try:
            # Correctly locate the data directory relative to the current file's parent
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
        # Sort keys by length, descending, to match longer phrases first
        sorted_phrases = sorted(self.pirate_dict.keys(), key=len, reverse=True)
        for phrase in sorted_phrases:
            # Use regex for whole-word matching, case-insensitive
            text = re.sub(r'\b' + re.escape(phrase) + r'\b', self.pirate_dict[phrase], text, flags=re.IGNORECASE)
        exclamations = ["Arrr!", "Shiver me timbers!", "Yo ho ho!", "Blimey!"]
        return f"{text} {random.choice(exclamations)}"

    def cog_unload(self):
        self.bot.tree.remove_command(self.translate_message_menu.name, type=self.translate_message_menu.type)
        self.bot.tree.remove_command(self.add_to_dictionary_menu.name, type=self.add_to_dictionary_menu.type)
        self.bot.tree.remove_command(self.report_translation_menu.name, type=self.report_translation_menu.type)

    async def perform_translation(self, original_message_content: str, target_lang: str, glossary: Optional[List[str]] = None, source_lang: Optional[str] = None):
        if not self.translator.is_initialized:
            return {"translated_text": "Translation service is currently unavailable.", "detected_language_code": "error"}
        
        # Pre-check to ignore messages that are exact glossary terms
        if glossary and original_message_content.strip().lower() in [term.lower() for term in glossary]:
            log.info(f"Auto-translate skipped: Message content '{original_message_content}' is a protected glossary term.")
            return {"translated_text": original_message_content, "detected_language_code": source_lang or "glossary"}

        if self.usage.check_limit_exceeded(len(original_message_content)):
            return {"translated_text": "The monthly translation limit has been reached.", "detected_language_code": "error"}

        # Sanitize the target language code
        lang_code_match = re.search(r'\b([a-z]{2}(?:-[A-Z]{2})?)\b', target_lang)
        sanitized_lang = lang_code_match.group(1) if lang_code_match else target_lang

        # Perform the translation
        translation_result = await self.translator.translate_text(original_message_content, sanitized_lang, glossary=glossary, source_language=source_lang)
        
        if translation_result and translation_result.get('translated_text') and translation_result.get("detected_language_code") != "error":
            if translation_result.get('translated_text') != original_message_content:
                await self.usage.record_usage(len(original_message_content))
                
        return translation_result

    async def translate_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        """Logic for the 'Translate Message' context menu."""
        await interaction.response.defer(ephemeral=True)
        if not message.content and not message.embeds:
            await interaction.followup.send("This message has no text or embeds to translate.", ephemeral=True)
            return
            
        target_language = await self.db.get_user_preferences(interaction.user.id)
        if not target_language:
            await interaction.followup.send("I don't know your preferred language yet! Use `/set_language` to set it up.", ephemeral=True)
            return
        
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
        
        if not translated_text and not translated_embeds:
            await interaction.followup.send("An error occurred during translation.", ephemeral=True)
            return

        # If only text was translated, send it in a simple embed
        if translated_text and not translated_embeds:
            reply_embed = discord.Embed(title="Translation Result", description=translated_text, color=discord.Color.blue())
            reply_embed.set_footer(text=f"Original message by {message.author.display_name}")
            await interaction.followup.send(embed=reply_embed, ephemeral=True)
        else: # Otherwise, send the text and/or the translated embeds
            await interaction.followup.send(translated_text or None, embeds=translated_embeds, ephemeral=True)

    async def add_to_dictionary_callback(self, interaction: discord.Interaction, message: discord.Message):
        """Logic for the 'Add to Dictionary' context menu."""
        modal = GlossaryEntryModal(self.db)
        if message.content:
            # Pre-fill the modal with the message content
            modal.term_input.default = message.content.strip()
        await interaction.response.send_modal(modal)

    async def report_translation_callback(self, interaction: discord.Interaction, message: discord.Message):
        """Logic for the 'Report Translation' context menu."""
        log.warning(f"User {interaction.user} reported a translation for message ID {message.id}.")
        # In the future, this could log the message ID, content, and user to a database for review.
        await interaction.response.send_message("Thank you for your feedback. The translation has been reported for review.", ephemeral=True)

    async def _get_webhook(self, channel: discord.TextChannel) -> Optional[discord.Webhook]:
        if channel.id in self.webhook_cache:
            return self.webhook_cache[channel.id]
        try:
            webhooks = await channel.webhooks()
            # Find a webhook managed by us, or create a new one.
            webhook = discord.utils.get(webhooks, name="Relay Translator")
            if webhook is None:
                webhook = await channel.create_webhook(name="Relay Translator", reason="For message impersonation")
            self.webhook_cache[channel.id] = webhook
            return webhook
        except discord.Forbidden:
            log.error(f"Missing 'Manage Webhooks' permission in #{channel.name} for impersonation.")
            return None
        except Exception as e:
            log.error(f"Failed to get/create webhook for #{channel.name}: {e}", exc_info=True)
            return None
    
    async def _send_corrected_message(self, original_message: discord.Message, corrected_text: str):
        """Uses a webhook to send the corrected text, impersonating the original author."""
        webhook = await self._get_webhook(original_message.channel)
        # Fallback to a simple message if webhook fails
        if not webhook:
            await original_message.channel.send(f"{original_message.author.mention} (corrected): {corrected_text}")
            return
        try:
            await webhook.send(
                content=corrected_text,
                username=original_message.author.display_name,
                avatar_url=original_message.author.display_avatar.url,
                allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.NotFound):
             await original_message.channel.send(f"{original_message.author.mention} (corrected): {corrected_text}")

    async def _send_webhook_as_reply(self, message: discord.Message, content: str):
        webhook = await self._get_webhook(message.channel)
        if not webhook:
            await message.reply(content, mention_author=False) # Fallback to normal reply
            return
        try:
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
        except Exception as e:
            log.error(f"Failed to set user language preference: {e}", exc_info=True)
            await interaction.response.send_message("An error occurred while saving your preference.", ephemeral=True)

    def _is_likely_english_slang(self, text: str) -> bool:
        """A heuristic pre-filter to catch common chat slang before hitting the API."""
        lower_text = text.lower().strip()
        
        # Whitelist of common short words that should NOT be considered slang.
        common_short_words = {"a", "i", "an", "as", "at", "be", "by", "do", "go", "he", "if", "in", "is", "it", "me", "my", "no", "of", "on", "or", "so", "to", "up", "us", "we", "am", "are", "and", "but", "can", "did", "for", "get", "has", "had", "him", "her", "how", "let", "not", "out", "say", "see", "she", "the", "try", "use", "was", "way", "who", "why", "you", "all", "any", "boy", "car", "day", "eat", "fly", "guy", "hey", "his", "its", "leg", "man", "new", "one", "our", "run", "sit", "ten", "too", "two", "war", "yet"}

        # Rule 1: Check for very short, common chat slang.
        if lower_text in ["ok", "lol", "ty", "thanks", "omg", "heh", "okey", "thx", "np", "gg", "gn", "gm", "brb", "wyd"]:
            return True

        # Rule 2: Check for single, short words that are NOT common English words
        if " " not in lower_text and len(lower_text) <= 3 and lower_text not in common_short_words:
            return True
            
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Standard checks to ignore bots, webhooks, DMs, etc.
        if message.author.bot or message.webhook_id or not message.guild or not isinstance(message.channel, discord.TextChannel) or not message.content:
            return
            
        if self._is_likely_english_slang(message.content):
            log.info(f"Auto-translate skipped: Heuristic pre-filter identified message '{message.content}' as likely slang.")
            return

        # --- Fuzzy Matching for Auto-Correction Suggestions ---
        glossary = await self.db.get_glossary_terms(message.guild.id)
        if glossary:
            # Use process.extractOne to find the best match from the glossary list
            best_match, score = process.extractOne(message.content.lower(), glossary, scorer=fuzz.ratio)
            
            SIMILARITY_THRESHOLD = 88 # High threshold to avoid false positives
            if score >= SIMILARITY_THRESHOLD and score < 100: # score < 100 avoids flagging exact matches
                log.info(f"Found close glossary match for '{message.content}': '{best_match}' (Score: {score}). Creating correction thread.")
                try:
                    thread_name = f"Correction for {message.author.display_name}"
                    # Ensure the bot has permission to create threads
                    if message.channel.permissions_for(message.guild.me).create_private_threads:
                        thread = await message.create_thread(name=thread_name)
                        view = CorrectionView(self, message, best_match)
                        await thread.send(f"Did you mean: `{best_match}`?", view=view)
                    else:
                        log.warning(f"Missing 'Create Private Threads' permission in #{message.channel.name}. Cannot create correction thread.")

                except Exception as e:
                    log.error(f"An unexpected error occurred during correction thread creation: {e}", exc_info=True)
                
                return # Stop further processing to avoid translating a potential typo

        # --- Translation Rule Hierarchy ---
        config = await self.db.get_auto_translate_config(message.channel.id)

        if not config:
            if await self.db.is_channel_exempt(message.channel.id):
                return
            
            guild_config = await self.db.get_guild_config(message.guild.id)
            server_lang = guild_config.get('server_wide_language') if guild_config else None
            
            if server_lang:
                config = {
                    'target_language_code': server_lang,
                    'impersonate': guild_config.get('sw_impersonate', False),
                    'delete_original': guild_config.get('sw_delete_original', False)
                }
            else:
                return # No channel or server rule exists
        
        target_lang = config['target_language_code']
        
        try:
            # Offline language detection to avoid translating messages already in the target language
            detected_lang_obj = self.detector.detect_language_of(message.content)
            if detected_lang_obj:
                detected_lang_code = detected_lang_obj.name.lower().replace("_", "-")
                # Compare base languages (e.g., 'en' from 'en-us' and 'en-gb')
                if detected_lang_code.split('-')[0] == target_lang.split('-')[0]:
                    return
        except Exception:
            # If detection fails, let the translation API handle it
            pass

        translation_result = await self.perform_translation(message.content, target_lang, glossary=glossary)
        if not translation_result: return

        translated_text = translation_result.get('translated_text')
        
        # Final check: Don't post if translation failed or is identical to the original
        if not translated_text or translation_result.get('detected_language_code') == "error" or translated_text == message.content:
            return
        
        # Post the translation
        if config.get('impersonate', False):
            await self._send_webhook_as_reply(message, translated_text)
        else:
            await message.reply(content=translated_text, mention_author=False)

        if config.get('delete_original', False):
            try:
                await message.delete()
            except discord.Forbidden:
                log.warning(f"Failed to delete original message {message.id}: Missing 'Manage Messages' permission.")
            except discord.NotFound:
                pass # Message was already deleted
                
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Ignore reactions from bots
        if payload.user_id == self.bot.user.id or (payload.member and payload.member.bot):
            return
            
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)): return
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        # --- Pirate Speak Feature ---
        if str(payload.emoji) == '🏴‍☠️':
            if message.content:
                log.info(f"Pirate speak triggered by {payload.member.display_name if payload.member else 'Unknown User'}.")
                pirate_text = self._translate_to_pirate_speak(message.content)
                await message.reply(content=pirate_text, mention_author=False)
            return
            
        target_language = self.emoji_to_language_map.get(str(payload.emoji))
        if not target_language or (not message.content and not message.embeds):
            return

        detected_lang_hint = None
        if message.content:
            try:
                # Use offline detection to pre-filter and provide a hint to the API
                detected_lang_obj = self.detector.detect_language_of(message.content)
                if detected_lang_obj:
                    detected_lang_code = detected_lang_obj.name.lower().replace("_", "-")
                    if detected_lang_code.split('-')[0] == target_language.split('-')[0]:
                        log.info(f"Flag reaction skipped: Offline pre-filter detected source '{detected_lang_code}' matches target '{target_language}'.")
                        return
                    detected_lang_hint = detected_lang_code
            except Exception:
                pass # Let the API handle detection if offline fails

        log.info(f"Flag reaction translation triggered by {payload.member.display_name if payload.member else 'Unknown User'} for language '{target_language}'.")
        async with channel.typing():
            glossary = await self.db.get_glossary_terms(payload.guild_id) if payload.guild_id else []

            translated_text = ""
            if message.content:
                # Pass the hint to the translation function to potentially save an API call
                translation_result = await self.perform_translation(message.content, target_language, glossary=glossary, source_lang=detected_lang_hint)
                if translation_result:
                    translated_text = translation_result.get('translated_text', '')

            translated_embeds = []
            if message.embeds:
                for embed in message.embeds:
                    translated_embed = await HubManagerCog._translate_embed(self.translator, embed, target_language, glossary=glossary)
                    translated_embeds.append(translated_embed)
                    
            if translated_text or translated_embeds:
                # Use ephemeral reply to avoid cluttering chat
                replying_user = self.bot.get_user(payload.user_id)
                if replying_user:
                    try:
                        await replying_user.send(f"Translation for the message in #{channel.name}:", content=translated_text or None, embeds=translated_embeds)
                    except discord.Forbidden:
                         # Fallback to public reply if DMs are closed
                         await message.reply(content=translated_text or None, embeds=translated_embeds, mention_author=False)
                else:
                    await message.reply(content=translated_text or None, embeds=translated_embeds, mention_author=False)


async def setup(bot: commands.Bot):
    # Ensure core services are attached to the bot object before loading the cog
    if not all(hasattr(bot, attr) for attr in ['db_manager', 'translator', 'usage_manager']):
        log.critical("TranslationCog cannot be loaded: Core services not found on bot object.")
        return
    await bot.add_cog(TranslationCog(bot, bot.db_manager, bot.translator, bot.usage_manager))
    log.info("TRANSLATION_COG: Cog loaded successfully.")