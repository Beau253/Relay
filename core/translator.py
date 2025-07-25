# core/translator.py

import os
import asyncio
import logging
import json
from typing import Optional, Dict
from google.cloud import translate_v3 as translate
from google.oauth2 import service_account
from google.api_core import exceptions as google_exceptions

log = logging.getLogger(__name__)

class TextTranslator:
    """
    A wrapper for the Google Cloud Translation API (v3).
    This class is designed to have its client 'hot-swapped' by a pool manager
    to allow for on-the-fly rotation between Google Cloud Projects.
    """
    def __init__(self):
        """Initializes the translator in a disabled state.
        Call initialize_client to make it active."""
        self.client: Optional[translate.TranslationServiceClient] = None
        self.parent: Optional[str] = None
        self.is_initialized = False

    async def initialize_client(self, project_details: Dict[str, str]):
        """
        Initializes or re-initializes the translation client with specific
        project credentials. This is called by the GoogleProjectPoolManager.
        
        Args:
            project_details: A dict containing 'id' and 'path' for the GCP project.
        """
        project_id = project_details.get("id")
        credential_source = project_details.get("source")
        creds_are_paths = project_details.get("is_path", True) # Get the flag from the details

        if not project_id or not credential_source:
            log.error("Cannot initialize translator: project_id or credential_source missing.")
            self.is_initialized = False
            return

        try:
            log.info(f"Initializing translation client for project: {project_id}")
            
            if creds_are_paths:
                # Local development: load from file path
                self.client = translate.TranslationServiceClient.from_service_account_file(credential_source)
            else:
                # Production (Render): load from environment variable content
                cred_json_str = os.getenv(credential_source)
                if not cred_json_str:
                    raise ValueError(f"Environment variable {credential_source} is not set.")
                
                cred_info = json.loads(cred_json_str)
                credentials = service_account.Credentials.from_service_account_info(cred_info)
                self.client = translate.TranslationServiceClient(credentials=credentials)

            self.parent = f"projects/{project_id}"
            self.is_initialized = True
            log.info(f"Google Translation client is now active for project: {project_id}")
        except FileNotFoundError:
            log.error(f"Credential file not found at path: {credential_source}. Translator is disabled.")
            self.is_initialized = False
        except (ValueError, json.JSONDecodeError) as e:
            log.error(f"Failed to parse credentials from env var {credential_source}: {e}", exc_info=True)
            self.is_initialized = False
        except Exception as e:
            log.error(f"Failed to initialize Google Translation client for project {project_id}: {e}", exc_info=True)
            self.is_initialized = False

    async def translate_text(
        self,
        text: str,
        target_language: str,
        source_language: Optional[str] = None
    ) -> Optional[str]:

        if not self.is_initialized or not self.client or not self.parent:
            log.error("Cannot translate: Translator service is not initialized or configured properly.")
            return None

        # --- Language Code Mapping & Debugging ---
        # If the target is Traditional Chinese, we remap it to the generic 'zh'
        # code for better compatibility with the Google API.
        effective_target_language = target_language
        if target_language == 'zh-TW':
            effective_target_language = 'zh'
            log.info(f"Remapping target language 'zh-TW' to 'zh' for API call.")

        # --- Skip translating if source and target are the same ---
        if source_language and effective_target_language and source_language.split('-')[0] == effective_target_language.split('-')[0]:
            log.info(f"Skipping translation: Source ('{source_language}') and target ('{effective_target_language}') are effectively the same.")
            return text

        loop = asyncio.get_running_loop()

        # Log the exact parameters we are about to send to the API
        api_params = {
            "parent": self.parent,
            "contents": [text],
            "target_language_code": effective_target_language,
            "source_language_code": source_language,
            "mime_type": "text/plain",
        }
        log.info(f"Calling Google Translate API with params: {api_params}")

        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.client.translate_text(**api_params)
            )

            if response and response.translations:
                translated_text = response.translations[0].translated_text
                log.info(f"Translation successful. Result: '{translated_text[:50]}...'")
                return translated_text
            else:
                log.warning(f"Translation to '{effective_target_language}' succeeded but API returned no translations.")
                return None

        except Exception as e:
            log.error(f"An error occurred during translation to '{effective_target_language}': {e}", exc_info=True)
            return None