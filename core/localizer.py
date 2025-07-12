# core/localizer.py

import discord
from discord import app_commands
from discord.app_commands import locale_str, TranslationContext, Translator
import json
import os
import logging

log = logging.getLogger(__name__)

class BotLocalizer(Translator):
    def __init__(self):
        self.translations = {}
        locale_dir = 'locale'
        if os.path.isdir(locale_dir):
            for filename in os.listdir(locale_dir):
                if filename.endswith('.json'):
                    lang_code = filename[:-5]
                    try:
                        with open(os.path.join(locale_dir, filename), 'r', encoding='utf-8') as f:
                            self.translations[lang_code] = json.load(f)
                            log.info(f"Loaded localization file: {filename}")
                    except Exception as e:
                        log.error(f"Failed to load localization file {filename}: {e}")

    async def translate(self, string: locale_str, locale: discord.Locale, context: TranslationContext) -> str | None:
        """
        Translates ONLY context menu commands.
        All other commands (e.g., slash commands) are ignored.
        """
        
        # 1. Only translate if the command is a Context Menu. Ignore slash commands.
        if not isinstance(context.data, app_commands.ContextMenu):
            return None

        # 2. Now proceed with the original logic, knowing it's a context menu.
        key = f"ContextMenu-name-{string.message}"
    
        locale_str_val = str(locale)
    
        # Check for full locale (e.g., 'en-US')
        if locale_str_val in self.translations and key in self.translations[locale_str_val]:
            return self.translations[locale_str_val].get(key)

        # Fallback to base language (e.g., 'en' for 'en-US')
        base_lang = str(locale).split('-')[0]
    
        if base_lang in self.translations and key in self.translations[base_lang]:
            return self.translations[base_lang].get(key)
    
        return None