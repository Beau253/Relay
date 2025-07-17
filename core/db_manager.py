# core/db_manager.py

import os
import asyncpg
import logging
import json # Ensure json is imported for JSONB handling
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

log = logging.getLogger(__name__)

# --- Database Table Creation SQL ---
# This dictionary holds all SQL for creating tables.
# Each entry will be executed if the table does not already exist.
TABLE_CREATION_SQL = {
    'translation_hubs': """
        CREATE TABLE IF NOT EXISTS translation_hubs (
            thread_id BIGINT PRIMARY KEY,
            source_channel_id BIGINT NOT NULL,
            guild_id BIGINT NOT NULL,
            language_code TEXT NOT NULL,
            creator_id BIGINT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            warning_sent BOOLEAN DEFAULT FALSE,
            is_archived BOOLEAN DEFAULT FALSE
        );
    """,
    'user_preferences': """
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id BIGINT PRIMARY KEY,
            user_locale TEXT NOT NULL
        );
    """,
    'bot_state': """
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value JSONB
        );
    """,
    'guild_configs': """
        CREATE TABLE IF NOT EXISTS guild_configs (
            guild_id BIGINT PRIMARY KEY,
            onboarding_channel_id BIGINT,
            admin_log_channel_id BIGINT,
            language_setup_role_id BIGINT,
            main_language_code TEXT DEFAULT 'en'
        );
    """
}

# --- DatabaseManager Class ---
class DatabaseManager:
    def __init__(self):
        self.db_url = os.getenv("DATABASE_URL")
        self.pool: Optional[asyncpg.pool.Pool] = None
        self.is_initialized = False

    async def initialize(self):
        """Initializes the database connection pool and ensures all necessary tables exist."""
        if not self.db_url:
            log.critical("DATABASE_URL environment variable not set. Cannot connect to database.")
            return

        try:
            self.pool = await asyncpg.create_pool(self.db_url)
            log.info("Database connection pool created successfully.")

            # Verify/create tables
            async with self.pool.acquire() as conn:
                ########
                # MODIFIED SECTION START - Table Creation Loop
                # Ensures all tables defined in TABLE_CREATION_SQL are created.
                #######
                for table_name, sql in TABLE_CREATION_SQL.items():
                    await conn.execute(sql)
                    log.info(f"Database table '{table_name}' verified/created.")
                #######
                # MODIFIED SECTION END - Table Creation Loop
                #######
            
            self.is_initialized = True
            log.info("DatabaseManager initialized successfully.")

        except Exception as e:
            log.critical(f"Failed to initialize DatabaseManager: {e}", exc_info=True)
            self.is_initialized = False

    async def close(self):
        """Closes the database connection pool."""
        if self.pool:
            await self.pool.close()
            log.info("Database connection pool closed.")
            self.is_initialized = False

    # --- Bot State Methods ---

    async def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.pool: return None
        try:
            async with self.pool.acquire() as conn:
                result = await conn.fetchval("SELECT value FROM bot_state WHERE key = $1;", key)
                return json.loads(result) if result else None
        except Exception as e:
            log.error(f"Error fetching bot state for key '{key}': {e}")
            return None

    async def set_state(self, key: str, value: Dict[str, Any]):
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                query = "INSERT INTO bot_state (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
                await conn.execute(query, key, json.dumps(value))
        except Exception as e:
            log.error(f"Error setting bot state for key '{key}': {e}")

    # --- Hub Management Methods (No changes here, preserving previous fixes) ---
    async def create_hub_record(self, thread_id: int, source_channel_id: int, guild_id: int, language_code: str, creator_id: int, expires_at: datetime):
        """Creates or updates a translation hub record."""
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO translation_hubs (thread_id, source_channel_id, guild_id, language_code, creator_id, expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (thread_id) DO UPDATE
                    SET source_channel_id = EXCLUDED.source_channel_id,
                        guild_id = EXCLUDED.guild_id,
                        language_code = EXCLUDED.language_code,
                        creator_id = EXCLUDED.creator_id,
                        expires_at = EXCLUDED.expires_at,
                        warning_sent = FALSE, -- Reset warning on reactivation
                        is_archived = FALSE; -- Unarchive on reactivation
                    """,
                    thread_id, source_channel_id, guild_id, language_code, creator_id, expires_at
                )
        except Exception as e:
            log.error(f"Error creating/updating hub record for thread {thread_id}: {e}")

    async def get_hub_by_thread_id(self, thread_id: int) -> Optional[asyncpg.Record]:
        """Fetches a single hub record by its thread ID."""
        if not self.pool: return None
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchrow("SELECT * FROM translation_hubs WHERE thread_id = $1;", thread_id)
        except Exception as e:
            log.error(f"Error fetching hub by thread ID {thread_id}: {e}")
            return None

    async def get_active_hub(self, source_channel_id: int, language_code: str) -> Optional[asyncpg.Record]:
        """Fetches an active hub record for a given source channel and language."""
        if not self.pool: return None
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchrow(
                    "SELECT * FROM translation_hubs WHERE source_channel_id = $1 AND language_code = $2 AND is_archived = FALSE;",
                    source_channel_id, language_code
                )
        except Exception as e:
            log.error(f"Error fetching active hub for channel {source_channel_id} and lang {language_code}: {e}")
            return None

    async def get_archived_hub(self, source_channel_id: int, language_code: str) -> Optional[asyncpg.Record]:
        """Fetches an archived hub record for a given source channel and language."""
        if not self.pool: return None
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchrow(
                    "SELECT * FROM translation_hubs WHERE source_channel_id = $1 AND language_code = $2 AND is_archived = TRUE;",
                    source_channel_id, language_code
                )
        except Exception as e:
            log.error(f"Error fetching archived hub for channel {source_channel_id} and lang {language_code}: {e}")
            return None

    async def get_hubs_by_source_channel(self, source_channel_id: int) -> List[asyncpg.Record]:
        """Fetches all active hubs associated with a given source channel."""
        if not self.pool: return []
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetch("SELECT * FROM translation_hubs WHERE source_channel_id = $1 AND is_archived = FALSE;", source_channel_id)
        except Exception as e:
            log.error(f"Error fetching hubs by source channel {source_channel_id}: {e}")
            return []

    async def get_hubs_needing_warning(self) -> List[asyncpg.Record]:
        """Fetches hubs that are active and nearing expiration, and a warning hasn't been sent."""
        if not self.pool: return []
        try:
            ten_mins_from_now = datetime.now(timezone.utc) + timedelta(minutes=10)
            async with self.pool.acquire() as conn:
                return await conn.fetch(
                    "SELECT * FROM translation_hubs WHERE expires_at < $1 AND warning_sent = FALSE AND is_archived = FALSE;",
                    ten_mins_from_now
                )
        except Exception as e:
            log.error(f"Error fetching hubs needing warning: {e}")
            return []

    async def update_hub_expiry(self, thread_id: int, new_expires_at: datetime) -> bool:
        """Updates the expiration time of a hub and resets warning status."""
        if not self.pool: return False
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE translation_hubs SET expires_at = $1, warning_sent = FALSE, is_archived = FALSE WHERE thread_id = $2;",
                    new_expires_at, thread_id
                )
                return result == 'UPDATE 1' # Returns true if one row was updated
        except Exception as e:
            log.error(f"Error updating hub expiry for thread {thread_id}: {e}")
            return False

    async def mark_hub_warning_sent(self, thread_id: int):
        """Marks a hub as having had an expiration warning sent."""
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE translation_hubs SET warning_sent = TRUE WHERE thread_id = $1;", thread_id)
        except Exception as e:
            log.error(f"Error marking hub {thread_id} warning sent: {e}")

    async def archive_hub(self, thread_id: int):
        """Archives a translation hub."""
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE translation_hubs SET is_archived = TRUE WHERE thread_id = $1;", thread_id)
        except Exception as e:
            log.error(f"Error archiving hub {thread_id}: {e}")

    async def delete_hub(self, thread_id: int):
        """Deletes a translation hub record from the database."""
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM translation_hubs WHERE thread_id = $1;", thread_id)
        except Exception as e:
            log.error(f"Error deleting hub {thread_id}: {e}")

    # --- User Locale/Preferences Methods ---
    async def set_user_preferences(self, user_id: int, user_locale: str):
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                query = "INSERT INTO user_preferences (user_id, user_locale) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET user_locale = EXCLUDED.user_locale;"
                await conn.execute(query, user_id, user_locale)
        except Exception as e:
            log.error(f"Error setting user preferences for user {user_id}: {e}")

    async def get_user_preferences(self, user_id: int) -> Optional[str]:
        if not self.pool: return None
        try:
            async with self.pool.acquire() as conn:
                record = await conn.fetchrow("SELECT user_locale FROM user_preferences WHERE user_id = $1;", user_id)
                return record['user_locale'] if record else None
        except Exception as e:
            log.error(f"Error fetching user preferences for user {user_id}: {e}")
            return None

    async def set_guild_config(self, guild_id: int, onboarding_channel_id: Optional[int] = None, admin_log_channel_id: Optional[int] = None, language_setup_role_id: Optional[int] = None, main_language_code: Optional[str] = None):
        """
        Sets or updates configuration settings for a specific guild.
        Parameters can be None to not update that specific setting.
        """
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                # Build the SQL dynamically based on provided parameters
                update_parts = []
                values = [guild_id]
                param_idx = 2 # Start from $2 for parameters in SET clause

                if onboarding_channel_id is not None:
                    update_parts.append(f"onboarding_channel_id = ${param_idx}")
                    values.append(onboarding_channel_id)
                    param_idx += 1
                if admin_log_channel_id is not None:
                    update_parts.append(f"admin_log_channel_id = ${param_idx}")
                    values.append(admin_log_channel_id)
                    param_idx += 1
                
                if main_language_code is not None:
                    update_parts.append(f"main_language_code = ${param_idx}")
                    values.append(main_language_code)
                    param_idx += 1
                if language_setup_role_id is not None:
                    update_parts.append(f"language_setup_role_id = ${param_idx}")
                    values.append(language_setup_role_id)
                    param_idx += 1
                if not update_parts:
                    log.warning(f"No guild config parameters provided for guild {guild_id}.")
                    return

                # Use ON CONFLICT to insert if not exists, or update if it does.
                # The $1 refers to guild_id in the VALUES clause.
                query = f"""
                    INSERT INTO guild_configs (guild_id, {', '.join([part.split(' = ')[0] for part in update_parts])})
                    VALUES ($1, {', '.join([f'${i}' for i in range(2, param_idx)])})
                    ON CONFLICT (guild_id) DO UPDATE SET {', '.join([f'{part.split(" = ")[0]} = EXCLUDED.{part.split(" = ")[0]}' for part in update_parts])};
                """
                
                await conn.execute(query, *values)
                log.info(f"Guild config updated for guild {guild_id}.")

        except Exception as e:
            log.error(f"Error setting guild config for guild {guild_id}: {e}", exc_info=True)


    async def get_guild_config(self, guild_id: int) -> Optional[asyncpg.Record]:
        """
        Retrieves configuration settings for a specific guild.
        Returns an asyncpg.Record or None if no config is found.
        """
        if not self.pool: return None
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchrow("SELECT * FROM guild_configs WHERE guild_id = $1;", guild_id)
        except Exception as e:
            log.error(f"Error fetching guild config for guild {guild_id}: {e}")
            return None