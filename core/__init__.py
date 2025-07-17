# core/__init__.py

# This file makes the 'core' directory a Python package.
# We can also use it to make imports more convenient.

from .db_manager import DatabaseManager
from .translator import TextTranslator
from .usage_manager import UsageManager
from .gcp_pool_manager import GoogleProjectPoolManager
from .error_handler import send_error_report
from .version import get_current_version
from .localizer import BotLocalizer
from .utils import language_autocomplete, SUPPORTED_LANGUAGES

# This __all__ list defines the public API of the package.
# When a user does `from core import *`, only these names will be imported.
__all__ = [
    "DatabaseManager",
    "TextTranslator",
    "UsageManager",
    "GoogleProjectPoolManager",
    "send_error_report",
    "get_current_version",
    "BotLocalizer"
]