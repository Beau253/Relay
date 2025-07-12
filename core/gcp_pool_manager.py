# core/gcp_pool_manager.py

import os
import asyncio
import logging
from typing import Dict, List, Optional

from core.db_manager import DatabaseManager
# We forward-declare the type to avoid a circular import error at runtime
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.translator import TextTranslator

log = logging.getLogger(__name__)

# The key for storing the GCP pool state in the database
GCP_POOL_STATE_KEY = "gcp_pool_state"

class GoogleProjectPoolManager:
    """
    Manages a pool of Google Cloud Projects to enable on-the-fly rotation
    when API usage for one project reaches a defined threshold.
    """
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        
        project_ids_str = os.getenv("GOOGLE_PROJECT_IDS", "")
        self.project_ids: List[str] = [pid.strip() for pid in project_ids_str.split(',') if pid.strip()]
        
        cred_paths_str = os.getenv("GOOGLE_APPLICATION_CREDENTIAL_PATHS")
        cred_vars_str = os.getenv("GOOGLE_APPLICATION_CREDENTIAL_VARS")

        if cred_paths_str:
            self.credential_sources: List[str] = [path.strip() for path in cred_paths_str.split(',') if path.strip()]
            self.creds_are_paths = True
            log.info("Loading GCP credentials from file paths.")
        elif cred_vars_str:
            self.credential_sources: List[str] = [var.strip() for var in cred_vars_str.split(',') if var.strip()]
            self.creds_are_paths = False
            log.info("Loading GCP credentials from environment variables.")
        else:
            self.credential_sources = []
            self.creds_are_paths = True # Default, though it will be empty
        
        if len(self.project_ids) != len(self.credential_sources):
            raise ValueError("The number of GOOGLE_PROJECT_IDS must match the number of credential sources (paths or vars).")

        self.pool_state: Dict[str, int] = {}
        self.translator_instance: Optional["TextTranslator"] = None
        self._rotation_lock = asyncio.Lock()
        self.is_initialized = False

    async def initialize(self, translator: "TextTranslator"):
        """Initializes the manager, loads state, and configures the initial translator client."""
        if not self.project_ids:
            log.critical("FATAL: GOOGLE_PROJECT_IDS is not set. Cannot start GoogleProjectPoolManager.")
            return

        log.info(f"Initializing GoogleProjectPoolManager with {len(self.project_ids)} projects.")
        self.translator_instance = translator
        
        self.pool_state = await self.db.get_state(GCP_POOL_STATE_KEY) or {}
        if "active_project_index" not in self.pool_state:
            self.pool_state["active_project_index"] = 0
            await self.db.set_state(GCP_POOL_STATE_KEY, self.pool_state)

        # Configure the translator with the currently active project's credentials on startup
        active_project_details = self.get_active_project_details()
        await self.translator_instance.initialize_client(active_project_details)
        
        self.is_initialized = True
        log.info(f"GCP Pool Manager loaded. Active project index: {self.pool_state['active_project_index']}")

    def get_active_project_details(self) -> Dict[str, str]:
        """Returns the ID and credential path of the currently active project."""
        active_index = self.pool_state.get("active_project_index", 0)
        return {
            "id": self.project_ids[active_index],
            "source": self.credential_sources[active_index],
            "is_path": self.creds_are_paths
        }
        
    def get_all_project_ids(self) -> List[str]:
        """Returns a list of all project IDs in the pool."""
        return self.project_ids

    async def rotate_active_project(self) -> str:
        """
        Rotates to the next project in the pool, saves state, and re-initializes
        the translator client on the fly.
        """
        async with self._rotation_lock:
            current_index = self.pool_state.get("active_project_index", 0)
            
            # Check again inside the lock to prevent a race condition where two
            # coroutines try to rotate at the same time.
            if current_index != self.pool_state.get("active_project_index", 0):
                log.info("Another process already handled rotation. Skipping.")
                active_details = self.get_active_project_details()
                return active_details['id']

            next_index = (current_index + 1) % len(self.project_ids)
            log.warning(f"Rotating Google Cloud Project from index {current_index} to {next_index}.")
            
            self.pool_state["active_project_index"] = next_index
            await self.db.set_state(GCP_POOL_STATE_KEY, self.pool_state)

            new_project_details = self.get_active_project_details()
            
            # Hot-swap the client in the translator instance
            if self.translator_instance:
                await self.translator_instance.initialize_client(new_project_details)
            else:
                log.error("Cannot re-initialize translator: instance not found.")

            log.info(f"Rotation complete. New active project is {new_project_details['id']}.")
            return new_project_details['id']