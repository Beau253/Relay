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
            expires_at TIMESTAMPTZ,
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
            main_language_code TEXT DEFAULT 'en',
            server_wide_language TEXT,
            sw_impersonate BOOLEAN DEFAULT TRUE,
            sw_delete_original BOOLEAN DEFAULT FALSE
        );
    """,
    'auto_translate_channels': """
        CREATE TABLE IF NOT EXISTS auto_translate_channels (
            channel_id BIGINT PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            target_language_code TEXT NOT NULL,
            impersonate BOOLEAN DEFAULT TRUE,
            delete_original BOOLEAN DEFAULT FALSE
        );
    """,
    'auto_translate_exemptions': """
        CREATE TABLE IF NOT EXISTS auto_translate_exemptions (
            channel_id BIGINT PRIMARY KEY,
            guild_id BIGINT NOT NULL
        );
    """,
    'glossary_terms': """
        CREATE TABLE IF NOT EXISTS glossary_terms (
            guild_id BIGINT,
            term TEXT,
            PRIMARY KEY (guild_id, term)
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

    async def update_hub_expiry(self, thread_id: int, new_expires_at: Optional[datetime]) -> bool:
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

    async def set_guild_config(self, guild_id: int, onboarding_channel_id: Optional[int] = None, admin_log_channel_id: Optional[int] = None, language_setup_role_id: Optional[int] = None, main_language_code: Optional[str] = None, server_wide_language: Optional[str] = None, **kwargs):
        """
        Sets or updates configuration settings for a specific guild.
        Accepts additional keyword arguments for boolean flags.
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
                if server_wide_language is not None:
                    update_parts.append(f"server_wide_language = ${param_idx}")
                    values.append(server_wide_language)
                    param_idx += 1
                
                # Check for the new boolean flags
                # We check `is not None` because a user might want to explicitly set them to False
                if 'sw_impersonate' in kwargs and kwargs['sw_impersonate'] is not None:
                    update_parts.append(f"sw_impersonate = ${param_idx}")
                    values.append(kwargs['sw_impersonate'])
                    param_idx += 1
                if 'sw_delete_original' in kwargs and kwargs['sw_delete_original'] is not None:
                    update_parts.append(f"sw_delete_original = ${param_idx}")
                    values.append(kwargs['sw_delete_original'])
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

    # --- Auto-Translate Channel Methods ---
    async def set_auto_translate_channel(self, channel_id: int, guild_id: int, target_language_code: str, impersonate: bool, delete_original: bool):
        """Sets or updates an auto-translate configuration for a channel."""
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                query = """
                    INSERT INTO auto_translate_channels (channel_id, guild_id, target_language_code, impersonate, delete_original)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (channel_id) DO UPDATE
                    SET target_language_code = EXCLUDED.target_language_code,
                        impersonate = EXCLUDED.impersonate,
                        delete_original = EXCLUDED.delete_original;
                """
                await conn.execute(query, channel_id, guild_id, target_language_code, impersonate, delete_original)
        except Exception as e:
            log.error(f"Error setting auto-translate channel for {channel_id}: {e}")

    async def remove_auto_translate_channel(self, channel_id: int):
        """Removes the auto-translate configuration for a channel."""
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM auto_translate_channels WHERE channel_id = $1;", channel_id)
        except Exception as e:
            log.error(f"Error removing auto-translate channel for {channel_id}: {e}")

    async def get_auto_translate_config(self, channel_id: int) -> Optional[asyncpg.Record]:
        """Gets the auto-translate configuration for a specific channel."""
        if not self.pool: return None
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchrow("SELECT * FROM auto_translate_channels WHERE channel_id = $1;", channel_id)
        except Exception as e:
            log.error(f"Error fetching auto-translate config for channel {channel_id}: {e}")
            return None
    
    async def get_all_auto_translate_configs_for_guild(self, guild_id: int) -> List[asyncpg.Record]:
        """Retrieves all auto-translate configurations for a guild."""
        if not self.pool: return []
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetch("SELECT * FROM auto_translate_channels WHERE guild_id = $1;", guild_id)
        except Exception as e:
            log.error(f"Error fetching all auto-translate configs for guild {guild_id}: {e}")
            return []

    # --- Auto-Translate Exemption Methods ---
    async def add_auto_translate_exemption(self, guild_id: int, channel_id: int):
        """Adds a channel to the auto-translate exemption list."""
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                query = "INSERT INTO auto_translate_exemptions (channel_id, guild_id) VALUES ($1, $2) ON CONFLICT (channel_id) DO NOTHING;"
                await conn.execute(query, channel_id, guild_id)
        except Exception as e:
            log.error(f"Error adding exemption for channel {channel_id}: {e}")

    async def remove_auto_translate_exemption(self, channel_id: int):
        """Removes a channel from the auto-translate exemption list."""
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM auto_translate_exemptions WHERE channel_id = $1;", channel_id)
        except Exception as e:
            log.error(f"Error removing exemption for channel {channel_id}: {e}")

    async def is_channel_exempt(self, channel_id: int) -> bool:
        """Checks if a channel is on the exemption list."""
        if not self.pool: return False
        try:
            async with self.pool.acquire() as conn:
                query = "SELECT EXISTS(SELECT 1 FROM auto_translate_exemptions WHERE channel_id = $1);"
                return await conn.fetchval(query, channel_id)
        except Exception as e:
            log.error(f"Error checking exemption for channel {channel_id}: {e}")
            return False

    async def get_exempt_channels(self, guild_id: int) -> List[asyncpg.Record]:
        """Gets all exempt channels for a guild."""
        if not self.pool: return []
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetch("SELECT * FROM auto_translate_exemptions WHERE guild_id = $1;", guild_id)
        except Exception as e:
            log.error(f"Error fetching exempt channels for guild {guild_id}: {e}")
            return []

    # --- Glossary Management Methods ---
    async def add_glossary_term(self, guild_id: int, term: str):
        """Adds a term to the server's 'do-not-translate' glossary."""
        if not self.pool: return
        try:
            # Terms are stored in lowercase to ensure case-insensitive matching.
            term_lower = term.lower()
            async with self.pool.acquire() as conn:
                query = "INSERT INTO glossary_terms (guild_id, term) VALUES ($1, $2) ON CONFLICT (guild_id, term) DO NOTHING;"
                await conn.execute(query, guild_id, term_lower)
        except Exception as e:
            log.error(f"Error adding glossary term '{term}' for guild {guild_id}: {e}")

    async def remove_glossary_term(self, guild_id: int, term: str):
        """Removes a term from the server's glossary."""
        if not self.pool: return
        try:
            term_lower = term.lower()
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM glossary_terms WHERE guild_id = $1 AND term = $2;", guild_id, term_lower)
        except Exception as e:
            log.error(f"Error removing glossary term '{term}' for guild {guild_id}: {e}")

    async def get_glossary_terms(self, guild_id: int) -> List[str]:
        """Gets a list of all glossary terms for a server."""
        if not self.pool: return []
        try:
            async with self.pool.acquire() as conn:
                # Fetch all records and extract just the 'term' column into a simple list.
                records = await conn.fetch("SELECT term FROM glossary_terms WHERE guild_id = $1;", guild_id)
                return [record['term'] for record in records]
        except Exception as e:
            log.error(f"Error fetching glossary terms for guild {guild_id}: {e}")
            return []