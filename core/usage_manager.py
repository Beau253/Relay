# core/usage_manager.py

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict
import json
from google.oauth2 import service_account
from google.cloud import monitoring_v3
from google.api_core import exceptions
from core.db_manager import DatabaseManager
from core.gcp_pool_manager import GoogleProjectPoolManager

log = logging.getLogger(__name__)

USAGE_STATE_KEY = "usage_tracker"

class UsageManager:
    """
    Manages API character usage tracking across multiple projects, enforces limits,
    triggers project rotation, and syncs with Google Cloud Monitoring.
    """
    def __init__(self, db_manager: DatabaseManager, gcp_pool_manager: GoogleProjectPoolManager):
        self.db = db_manager
        self.gcp_pool_manager = gcp_pool_manager
        
        self.limit = int(os.getenv("TRANSLATION_API_LIMIT", 500000))
        safety_factor = float(os.getenv("TRANSLATION_API_LIMIT_SAFETY_FACTOR", 0.98))
        self.safe_limit = int(self.limit * safety_factor)
        
        self.rotation_threshold = int(os.getenv("PROJECT_SWITCH_THRESHOLD", 490000))

        self.monitoring_clients: Dict[str, monitoring_v3.MetricServiceClient] = {}
        self.is_initialized = False

        self._usage_state: Dict = {}
        self._active_project_id: Optional[str] = None
        self._current_month: str = ""

    @property
    def characters_used_current_project(self) -> int:
        """Returns the character usage for the currently active project."""
        if not self._active_project_id:
            return 0
        return self._usage_state.get("usage_by_project", {}).get(self._active_project_id, 0)

    @property
    def total_characters_used(self) -> int:
        """Returns the total character usage summed across all projects for the month."""
        return sum(self._usage_state.get("usage_by_project", {}).values())

    @property
    def current_month(self) -> str:
        return self._current_month

    async def initialize(self):
        """Initializes the manager, loads state from DB, and sets up monitoring clients."""
        log.info("Initializing UsageManager...")
        if not self.db.is_initialized or not self.gcp_pool_manager.is_initialized:
            log.error("UsageManager cannot initialize: Dependencies (DB or GCP Pool) are not ready.")
            return

        self._active_project_id = self.gcp_pool_manager.get_active_project_details()['id']
        await self._load_state()
        log.info(f"UsageManager loaded state for month {self._current_month}.")
        log.info(f"Active project '{self._active_project_id}' usage: {self.characters_used_current_project} chars.")
        log.info(f"Total usage this month: {self.total_characters_used} chars.")
        
        for project_id, cred_source in zip(self.gcp_pool_manager.project_ids, self.gcp_pool_manager.credential_sources):
            try:
                if self.gcp_pool_manager.creds_are_paths:
                    # Local development: load from file path
                    client = monitoring_v3.MetricServiceClient.from_service_account_file(cred_source)
                else:
                    # Production (Render): load from environment variable content
                    cred_json_str = os.getenv(cred_source)
                    if not cred_json_str:
                        raise ValueError(f"Monitoring client: Environment variable {cred_source} is not set.")
                    
                    cred_info = json.loads(cred_json_str)
                    credentials = service_account.Credentials.from_service_account_info(cred_info)
                    client = monitoring_v3.MetricServiceClient(credentials=credentials)

                self.monitoring_clients[project_id] = client
                log.info(f"Google Monitoring client initialized for project: {project_id}")
            except Exception as e:
                log.error(f"Could not initialize Google Monitoring client for {project_id}: {e}. Sync will be disabled for this project.")
        
        self.is_initialized = True

    async def _load_state(self):
        """Loads usage data from the database and checks for month rollover."""
        now = datetime.now(timezone.utc)
        current_month_str = now.strftime("%Y-%m")
        
        state = await self.db.get_state(USAGE_STATE_KEY) or {}
        
        self._current_month = state.get("month", current_month_str)
        
        if self._current_month != current_month_str:
            log.info(f"New month detected. Resetting all usage counters from month {self._current_month}.")
            await self.reset_usage()
        else:
            self._usage_state = state

    async def _save_state(self):
        """Saves the current usage data to the database."""
        await self.db.set_state(USAGE_STATE_KEY, self._usage_state)

    def check_limit_exceeded(self, text_length: int = 0) -> bool:
        """
        Checks if the TOTAL usage across all projects exceeds the safe limit.
        Used by BotPoolManager for bot token rotation.
        """
        now = datetime.now(timezone.utc)
        if self._current_month != now.strftime("%Y-%m"):
            return False 
        return (self.total_characters_used + text_length) > self.safe_limit

    async def record_usage(self, character_count: int):
        """
        Adds characters to the active project's count and triggers rotation if needed.
        """
        if not self._active_project_id:
            log.error("Cannot record usage: No active project ID is set.")
            return
            
        usage_by_project = self._usage_state.setdefault("usage_by_project", {})
        current_usage = usage_by_project.get(self._active_project_id, 0)
        new_usage = current_usage + character_count
        usage_by_project[self._active_project_id] = new_usage
        
        await self._save_state()
        log.info(f"Recorded {character_count} chars for '{self._active_project_id}'. New total: {new_usage}/{self.rotation_threshold}")

        # --- Trigger Rotation Logic ---
        if new_usage >= self.rotation_threshold:
            log.warning(f"Project '{self._active_project_id}' usage threshold reached. Triggering rotation.")
            try:
                new_active_project_id = await self.gcp_pool_manager.rotate_active_project()
                self._active_project_id = new_active_project_id
                log.info(f"UsageManager has switched to new active project: {self._active_project_id}")
            except Exception as e:
                log.critical(f"An error occurred during project rotation trigger: {e}", exc_info=True)

    async def reset_usage(self):
        """Resets all usage counters to 0 for the current month."""
        now = datetime.now(timezone.utc)
        self._current_month = now.strftime("%Y-%m")
        self._usage_state = {
            "month": self._current_month,
            "usage_by_project": {project_id: 0 for project_id in self.gcp_pool_manager.get_all_project_ids()}
        }
        await self._save_state()
        log.info(f"All usage counters have been reset for the new month: {self._current_month}")

    async def sync_with_google(self):
        """
        Periodically syncs the local usage count for ALL projects with data from
        Google Cloud Monitoring.
        """
        await self._load_state()
        log.info("Performing scheduled sync with Google Cloud Monitoring for all projects...")
        now = datetime.now(timezone.utc)
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        drift_detected = False
        for project_id, client in self.monitoring_clients.items():
            try:
                project_name = f"projects/{project_id}"
                interval = monitoring_v3.TimeInterval(
                    {"end_time": {"seconds": int(now.timestamp())}, "start_time": {"seconds": int(start_of_month.timestamp())}}
                )
                metric_filter = (
                    'metric.type = "serviceruntime.googleapis.com/quota/rate/net_usage" AND '
                    'resource.type = "consumer_quota" AND '
                    'resource.label.service = "translate.googleapis.com" AND '
                    'metric.label.quota_metric = "translate.googleapis.com/default"'
                )

                results = client.list_time_series(
                    request={"name": project_name, "filter": metric_filter, "interval": interval, "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL}
                )

                google_total = sum(point.value.int64_value for series in results for point in series.points)
                local_total = self._usage_state.get("usage_by_project", {}).get(project_id, 0)

                if google_total != local_total:
                    log.info(f"Usage drift detected for project '{project_id}'. Local: {local_total}, Google: {google_total}. Correcting...")
                    self._usage_state.setdefault("usage_by_project", {})[project_id] = google_total
                    drift_detected = True
                else:
                    log.info(f"Usage for '{project_id}' is in sync. Current: {local_total}")

            except exceptions.NotFound:
                log.warning(f"Metric not found for '{project_id}'. This is normal if no usage this month. Setting local usage to 0.")
                if self._usage_state.get("usage_by_project", {}).get(project_id, 0) != 0:
                    self._usage_state.setdefault("usage_by_project", {})[project_id] = 0
                    drift_detected = True
            except Exception as e:
                log.error(f"Failed to sync usage for project '{project_id}': {e}", exc_info=True)
        
        if drift_detected:
            log.info("Saving corrected usage state to database after sync.")
            await self._save_state()
        else:
            log.info("Google sync complete. No usage drifts detected.")