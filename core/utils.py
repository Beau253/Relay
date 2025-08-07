# core/utils.py

import discord
from discord import app_commands
from typing import List

# This dictionary should be the single source of truth for supported languages.
SUPPORTED_LANGUAGES = {
    'en': 'English', 'es': 'Spanish', 'fr': 'French', 'de': 'German',
    'it': 'Italian', 'pt': 'Portuguese', 'ru': 'Russian', 'zh': 'Chinese (Simplified)',
    'zh-TW': 'Chinese (Traditional)', 'yue': 'Cantonese', 'ja': 'Japanese',
    'ko': 'Korean', 'ar': 'Arabic', 'hi': 'Hindi', 'id': 'Indonesian',
    'ms': 'Malay', 'vi': 'Vietnamese', 'ur': 'Urdu', 'nl': 'Dutch',
    'sv': 'Swedish', 'no': 'Norwegian', 'da': 'Danish', 'fi': 'Finnish',
    'pl': 'Polish', 'tr': 'Turkish'
}

async def language_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    """A shared autocomplete function for selecting a supported language."""
    choices = []
    for code, name in SUPPORTED_LANGUAGES.items():
        if current.lower() in name.lower() or current.lower() in code.lower():
            # The name is for display, the value is the critical part that the API needs.
            choices.append(app_commands.Choice(name=f"{name} ({code})", value=code))
    return choices[:25] # Limit to 25 choices, the maximum for autocomplete

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
    