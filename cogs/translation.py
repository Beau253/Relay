import discord
import logging
from discord.ext import commands
from discord import app_commands
from cogs.hub_manager import HubManagerCog

# Import our core services
from core import DatabaseManager, TextTranslator, UsageManager, language_autocomplete, SUPPORTED_LANGUAGES

log = logging.getLogger(__name__)

# A mapping from country flag emojis to ISO 639-1 language codes.
FLAG_TO_LANGUAGE = {
    '🇺🇸': 'en', '🇬🇧': 'en', '🇦🇺': 'en', # English
    '🇪🇸': 'es', '🇲🇽': 'es', '🇦🇷': 'es', # Spanish
    '🇫🇷': 'fr', '🇨🇦': 'fr', # French
    '🇩🇪': 'de', # German
    '🇮🇹': 'it', # Italian
    '🇵🇹': 'pt', '🇧🇷': 'pt', # Portuguese
    '🇷🇺': 'ru', # Russian
    '🇨🇳': 'zh', # Chinese (Simplified)
    '🇹🇼': 'zh-TW', # Chinese (Traditional)
    '🇭🇰': 'yue', # Cantonese
    '🇯🇵': 'ja', # Japanese
    '🇰🇷': 'ko', # Korean
    '🇸🇦': 'ar', # Arabic
    '🇮🇳': 'hi', # Hindi
    '🇮🇩': 'id', # Indonesian
    '🇲🇾': 'ms', # Malay
    '🇻🇳': 'vi', # Vietnamese
    '🇵🇰': 'ur', # Urdu
    '🇳🇱': 'nl', # Dutch
    '🇸🇪': 'sv', # Swedish
    '🇳🇴': 'no', # Norwegian
    '🇩🇰': 'da', # Danish
    '🇫🇮': 'fi', # Finnish
    '🇵🇱': 'pl', # Polish
    '🇹🇷': 'tr', # Turkish
}

@app_commands.guild_only()
class TranslationCog(commands.Cog, name="Translation"):
    def __init__(self, bot: commands.Bot, db_manager: DatabaseManager, translator: TextTranslator, usage_manager: UsageManager):
        self.bot = bot
        self.db = db_manager
        self.translator = translator
        self.usage = usage_manager

        log.info("[TRANSLATION_COG] Initializing and adding 'Translate Message' context menu...")
        self.translate_message_menu = app_commands.ContextMenu(
            name='Translate Message',
            callback=self.translate_message_callback,
        )
        self.bot.tree.add_command(self.translate_message_menu)
        log.info("[TRANSLATION_COG] 'Translate Message' context menu added to tree.")

    def cog_unload(self):
        log.info("[TRANSLATION_COG] Unloading and removing 'Translate Message' context menu.")
        self.bot.tree.remove_command(self.translate_message_menu.name, type=self.translate_message_menu.type)

    async def perform_translation(self, original_message_content: str, target_lang: str) -> str | None:
        if not self.translator.is_initialized:
            log.warning("Translation attempted but translator is not initialized.")
            return "Translation service is currently unavailable."
        
        if self.usage.check_limit_exceeded(len(original_message_content)):
            log.warning(f"Translation blocked: API usage limit of {self.usage.safe_limit} has been reached.")
            return "The monthly translation limit has been reached. Please try again next month."
        
        translated_text = await self.translator.translate_text(original_message_content, target_lang)

        if translated_text:
            await self.usage.record_usage(len(original_message_content))
        
        return translated_text

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
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or (payload.member and payload.member.bot):
            return

        target_language = FLAG_TO_LANGUAGE.get(str(payload.emoji))
        if not target_language:
            return

        try:
            channel = self.bot.get_channel(payload.channel_id)
            if not isinstance(channel, discord.TextChannel): return
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        if not message.content and not message.embeds:
            return

        log.info(f"Flag reaction translation triggered by {payload.member.name} for language '{target_language}'.")
        
        async with channel.typing():
            translated_text = ""
            if message.content:
                translated_text = await self.perform_translation(message.content, target_language)

            translated_embeds = []
            if message.embeds:
                for embed in message.embeds:
                    # Call the static helper method from the HubManagerCog
                    translated_embed = await HubManagerCog._translate_embed(self.translator, embed, target_language)
                    translated_embeds.append(translated_embed)
            
            # Send the reply with both text and embeds
            # The API handles cases where one or the other is empty gracefully
            await message.reply(
                content=translated_text,
                embeds=translated_embeds,
                mention_author=False
            )

    async def translate_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        """The actual logic for the 'Translate Message' context menu."""
        await interaction.response.defer(ephemeral=True)

        # Check if there is anything at all to translate
        if not message.content and not message.embeds:
            await interaction.followup.send("This message has no text or embeds to translate.")
            return

        target_language = await self.db.get_user_preferences(interaction.user.id)
        if not target_language:
            await interaction.followup.send("I don't know your preferred language yet! Please use /set_language to set it up.", ephemeral=True)
            return

        # --- NEW: Full translation logic for both text and embeds ---
        translated_text = ""
        if message.content:
            translated_text = await self.perform_translation(message.content, target_language)

        translated_embeds = []
        if message.embeds:
            for embed in message.embeds:
                # Call the static helper method from the HubManagerCog
                translated_embed = await HubManagerCog._translate_embed(self.translator, embed, target_language)
                translated_embeds.append(translated_embed)

        # If we only have text, send a simple embed.
        if translated_text and not translated_embeds:
            reply_embed = discord.Embed(
                title="Translation Result",
                description=translated_text,
                color=discord.Color.blue()
            )
            reply_embed.set_footer(text=f"Original message by {message.author.display_name}")
            await interaction.followup.send(embed=reply_embed)
        
        # If we have embeds (with or without text), send them all.
        elif translated_embeds:
            # Send the translated text first if it exists
            if translated_text:
                await interaction.followup.send(translated_text)
                # Use a subsequent followup for the embeds
                await interaction.followup.send(embeds=translated_embeds)
            else:
                # If only embeds, send them in the first followup
                await interaction.followup.send(embeds=translated_embeds)
        
        # Handle case where text translation failed but there were no embeds
        elif not translated_text and message.content:
             await interaction.followup.send("An error occurred during translation. Please try again.", ephemeral=True)

async def setup(bot: commands.Bot):
    """The setup function for the cog."""
    if not all(hasattr(bot, attr) for attr in ['db_manager', 'translator', 'usage_manager']):
        log.critical("TranslationCog cannot be loaded: Core services not found on bot object.")
        return
    
    await bot.add_cog(TranslationCog(bot, bot.db_manager, bot.translator, bot.usage_manager))
    log.info("TRANSLATION_COG: Cog loaded, context menu registered in __init__.")