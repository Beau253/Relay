import discord
import logging
import os
import json
from discord.ext import commands
from discord import app_commands
from cogs.hub_manager import HubManagerCog

# Import our core services and utilities
from core import DatabaseManager, TextTranslator, UsageManager, language_autocomplete, SUPPORTED_LANGUAGES
from core.utils import country_code_to_flag

log = logging.getLogger(__name__)

@app_commands.guild_only()
class TranslationCog(commands.Cog, name="Translation"):
    def __init__(self, bot: commands.Bot, db_manager: DatabaseManager, translator: TextTranslator, usage_manager: UsageManager):
        self.bot = bot
        self.db = db_manager
        self.translator = translator
        self.usage = usage_manager
        self.emoji_to_language_map: dict[str, str] = {}
        self._load_flag_data()

        log.info("[TRANSLATION_COG] Initializing and adding 'Translate Message' context menu...")
        self.translate_message_menu = app_commands.ContextMenu(
            name='Translate Message',
            callback=self.translate_message_callback,
        )
        self.bot.tree.add_command(self.translate_message_menu)
        log.info("[TRANSLATION_COG] 'Translate Message' context menu added to tree.")

    def _load_flag_data(self):
        """Loads flag data from flags.json and builds the emoji-to-language mapping."""
        try:
            # Construct the path to the data file
            # Assumes the 'data' directory is at the root of the project
            script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(script_dir, 'data', 'flags.json')
            
            with open(file_path, 'r', encoding='utf-8') as f:
                flag_data = json.load(f)

            for _, data in flag_data.items():
                country_code = data.get("countryCode")
                languages = data.get("languages")
                
                # We need a valid country code and at least one language
                if country_code and languages:
                    emoji = country_code_to_flag(country_code)
                    # Use the first language in the list as the target
                    self.emoji_to_language_map[emoji] = languages[0]
            
            log.info(f"Successfully loaded {len(self.emoji_to_language_map)} flag-to-language mappings.")

        except FileNotFoundError:
            log.error("Could not find data/flags.json. Flag reaction translations will not work.")
        except Exception as e:
            log.error(f"Error loading flags.json: {e}", exc_info=True)


    def cog_unload(self):
        log.info("[TRANSLATION_COG] Unloading and removing 'Translate Message' context menu.")
        self.bot.tree.remove_command(self.translate_message_menu.name, type=self.translate_message_menu.type)

    async def perform_translation(self, original_message_content: str, target_lang: str):
        if not self.translator.is_initialized:
            log.warning("Translation attempted but translator is not initialized.")
            return {"translated_text": "Translation service is currently unavailable.", "detected_language_code": "error"}
        
        if self.usage.check_limit_exceeded(len(original_message_content)):
            log.warning(f"Translation blocked: API usage limit of {self.usage.safe_limit} has been reached.")
            return {"translated_text": "The monthly translation limit has been reached. Please try again next month.", "detected_language_code": "error"}
        
        translation_result = await self.translator.translate_text(original_message_content, target_lang)

        if translation_result and translation_result.get('translated_text'):
            await self.usage.record_usage(len(original_message_content))
        
        return translation_result

    @app_commands.command(name="set_language", description="Set your preferred language for translations.")
    @app_commands.autocomplete(language=language_autocomplete)
    @app_commands.describe(language="The language you want messages to be translated into for you.")
    async def set_language(self, interaction: discord.Interaction, language: str):
        if language not in SUPPORTED_LANGUAGES:
            await interaction.response.send_message(
                f"Sorry, `{language}` is not a supported language code. Please choose from the list.",
                ephemeral=True
            )
            return

        try:
            await self.db.set_user_preferences(user_id=interaction.user.id, user_locale=language)
            log.info(f"User {interaction.user.id} manually set their language to '{language}'.")
            
            await interaction.response.send_message(
                f"Your preferred language has been set to **{SUPPORTED_LANGUAGES[language]}** (`{language}`).",
                ephemeral=True
            )
        except Exception as e:
            log.error(f"Failed to set language for user {interaction.user.id}: {e}", exc_info=True)
            await interaction.response.send_message(
                "An error occurred while saving your preference. Please try again later.",
                ephemeral=True
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listener for the auto-translate feature."""
        # Ignore messages from bots, webhooks, DMs, or with no content
        if message.author.bot or message.webhook_id or not message.guild or not message.content:
            return

        # Check if the channel is configured for auto-translation
        config = await self.db.get_auto_translate_config(message.channel.id)
        if not config:
            return

        log.info(f"Auto-translate triggered in channel {message.channel.id} for message {message.id}")
        
        target_lang = config['target_language_code']
        
        # Perform translation and get detected language
        translation_result = await self.perform_translation(message.content, target_lang)

        if not translation_result:
            log.warning(f"Auto-translation failed for message {message.id}.")
            return

        translated_text = translation_result.get('translated_text')
        detected_language = translation_result.get('detected_language_code')

        # Do not translate if the detected language is the same as the target
        if detected_language and detected_language.split('-')[0] == target_lang.split('-')[0]:
            return
            
        if not translated_text:
            return

        # Post as a reply
        await message.reply(
            content=translated_text,
            mention_author=False
        )


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        log.info(f"DEBUG: Received reaction. Emoji is: '{str(payload.emoji)}'. Map contains England flag?: {'🏴󠁧󠁢󠁥󠁮󠁧󠁿' in self.emoji_to_language_map}")
        if payload.user_id == self.bot.user.id or (payload.member and payload.member.bot):
            return

        # Use the dynamically loaded map instead of the hardcoded one
        target_language = self.emoji_to_language_map.get(str(payload.emoji))
        if not target_language:
            return

        try:
            channel = self.bot.get_channel(payload.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)): return
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        if not message.content and not message.embeds:
            return

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
                await message.reply(
                    content=translated_text,
                    embeds=translated_embeds,
                    mention_author=False
                )

    async def translate_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        """The actual logic for the 'Translate Message' context menu."""
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
            reply_embed = discord.Embed(
                title="Translation Result",
                description=translated_text,
                color=discord.Color.blue()
            )
            reply_embed.set_footer(text=f"Original message by {message.author.display_name}")
            await interaction.followup.send(embed=reply_embed)
        
        elif translated_embeds:
            if translated_text:
                await interaction.followup.send(translated_text, embeds=translated_embeds)
            else:
                await interaction.followup.send(embeds=translated_embeds)
        
        elif not translated_text and message.content:
             await interaction.followup.send("An error occurred during translation. Please try again.", ephemeral=True)

async def setup(bot: commands.Bot):
    """The setup function for the cog."""
    if not all(hasattr(bot, attr) for attr in ['db_manager', 'translator', 'usage_manager']):
        log.critical("TranslationCog cannot be loaded: Core services not found on bot object.")
        return
    
    await bot.add_cog(TranslationCog(bot, bot.db_manager, bot.translator, bot.usage_manager))
    log.info("TRANSLATION_COG: Cog loaded, context menu registered in __init__.")