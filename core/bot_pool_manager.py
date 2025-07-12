# core/bot_pool_manager.py

import os
import logging
from typing import List, Dict, Any

from core.db_manager import DatabaseManager
from core.usage_manager import UsageManager

log = logging.getLogger(__name__)

# The key we'll use to store pool data in our database's bot_state table
POOL_STATE_KEY = "bot_pool_state"

class ShutdownForBotRotation(Exception):
    """Custom exception raised to signal a graceful shutdown for bot rotation."""
    pass


class BotPoolManager:
    """
    Manages a pool of bot tokens to circumvent individual API limits.
    When the active bot's usage limit is reached, it signals the application
    to restart with the next available bot token.
    """
    def __init__(self, db_manager: DatabaseManager, usage_manager: UsageManager):
        self.db = db_manager
        self.usage_manager = usage_manager
        
        token_str = os.getenv("BOT_TOKENS", "")
        self.tokens: List[str] = [token.strip() for token in token_str.split(',') if token.strip()]
        
        self.pool_state: Dict[str, Any] = {}
        self.is_initialized = False

    async def initialize(self):
        """Initializes the manager, loading the pool state from the database."""
        if not self.tokens:
            log.critical("FATAL: BOT_TOKENS environment variable is not set or is empty. Cannot start BotPoolManager.")
            return

        log.info(f"Initializing BotPoolManager with {len(self.tokens)} bots in the pool.")
        if self.db.is_initialized:
            self.pool_state = await self.db.get_state(POOL_STATE_KEY) or {}
            
            # Set default state if it's the first time running
            if "active_token_index" not in self.pool_state:
                self.pool_state["active_token_index"] = 0
                await self.db.set_state(POOL_STATE_KEY, self.pool_state)

                await self.usage_manager.reset_usage()

                raise ShutdownForBotRotation(
                    f"Rotating from token index {current_index} to {next_index}."
                )
            
            self.is_initialized = True
            log.info(f"BotPoolManager loaded state. Active bot index: {self.pool_state['active_token_index']}")
        else:
            log.error("BotPoolManager cannot initialize: DatabaseManager is not ready.")
            return

    async def get_active_token(self) -> str:
        """
        Determines the correct bot token to use. If the current bot has
        exceeded its usage limit, it triggers a rotation and signals for shutdown.
        """
        if not self.is_initialized:
            raise RuntimeError("BotPoolManager is not initialized.")

        # Check if the current bot has hit its limit
        if self.usage_manager.check_limit_exceeded():
            log.warning("Usage limit exceeded for the current bot. Initiating rotation.")
            await self._rotate_to_next_bot()
        
        # Return the currently active token
        active_index = self.pool_state.get("active_token_index", 0)
        return self.tokens[active_index]

    async def _rotate_to_next_bot(self):
        """
        Updates the state to point to the next bot in the pool, resets the
        usage counter for the new bot, and raises an exception to trigger a restart.
        """
        current_index = self.pool_state.get("active_token_index", 0)
        next_index = (current_index + 1) % len(self.tokens)
        
        log.info(f"Rotating from bot index {current_index} to {next_index}.")
        
        # Update the database with the new active index
        self.pool_state["active_token_index"] = next_index
        await self.db.set_state(POOL_STATE_KEY, self.pool_state)
        
        # Crucially, reset the usage manager's state in the database for the new bot
        # This clears the character count for the incoming bot.
        await self.usage_manager.record_usage(-self.usage_manager._local_usage) # Resets local and DB counter to 0

        # Raise the special exception to signal the main runner to shut down
        raise ShutdownForBotRotation(
            f"Rotating from token index {current_index} to {next_index}."
        )